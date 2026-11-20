"""Verify user delivery policy in real fanout and dispatcher paths."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest

from hunter.application.cutover import CutoverMode
from hunter.application.dispatcher import SendResult
from hunter.bootstrap import Bootstrap
from hunter.core.config import Settings
from hunter.core.db import Database
from hunter.domain.delivery_policy import is_delivery_expired
from hunter.domain.observations import ObservationBatch, ObservedListing, ParseOutcome, PriceState
from hunter.storage.deliveries import ClaimedDelivery


class FakeSender:
    def __init__(self) -> None:
        self.user_ids: list[int] = []

    async def __call__(self, delivery: ClaimedDelivery) -> SendResult:
        self.user_ids.append(delivery.user_id)
        return SendResult("sent", f"policy-{len(self.user_ids)}")


@pytest.fixture
async def policy_system(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HUNTER_DATA_DIR", str(tmp_path))
    settings = Settings(env="test")
    settings.public_v2.enabled = True
    sender = FakeSender()
    db = Database(tmp_path / "delivery-policy.db")
    app = Bootstrap(settings=settings, db=db, senders={"telegram": sender})
    await app.startup()
    await app.dispatcher.stop()

    async def seed(conn: aiosqlite.Connection) -> None:
        await conn.execute(
            "INSERT INTO users (id, role, created_at) VALUES "
            "(10, 'user', datetime('now')), (20, 'user', datetime('now'))"
        )
        await conn.execute(
            "INSERT INTO channel_destinations (user_id, channel, address, verified) "
            "VALUES (10, 'telegram', '100', 1), (20, 'telegram', '200', 1)"
        )

    await app.writer.submit(seed)
    slot = await app.slot_repo.register_slot("policy-slot", "http://policy-slot:8080")
    one = await app.subscription_service.add_subscription(
        10, "https://avito.ru/moskva/noutbuki?pmax=100000", 100000, sub_id="policy-10"
    )
    two = await app.subscription_service.add_subscription(
        20, "https://avito.ru/moskva/noutbuki?pmax=100000", 100000, sub_id="policy-20"
    )
    assert one.query_id == two.query_id
    await app.cutover_mgr.set_tenant_mode(10, CutoverMode.V2)
    await app.cutover_mgr.set_tenant_mode(20, CutoverMode.V2)
    await app.public_poll_runtime.stop()
    row = await (await db._conn.execute(
        "SELECT generation FROM assignments WHERE slot_id = ? AND query_id = ? AND active = 1",
        (slot.slot_id, one.query_id),
    )).fetchone()
    assert row is not None
    yield app, sender, one.query_id, int(row[0])
    await app.shutdown()


async def ingest(app: Bootstrap, qid: str, generation: int, price: int, timestamp: str):
    batch = ObservationBatch(
        slot_id="policy-slot", query_id=qid, observed_at=timestamp,
        outcome=ParseOutcome.OK,
        listings=(ObservedListing(
            "policy-item", "Laptop", price, PriceState.ACTIVE,
            "https://avito.ru/item/policy",
        ),),
        assignment_generation=generation,
    )
    return await app.ingest_observation(batch, request_sequence=1 if price == 50000 else 2)


@pytest.mark.asyncio
async def test_quiet_hours_and_user_pause_apply_to_real_fanout(policy_system):
    app, sender, qid, generation = policy_system
    async def configure(conn: aiosqlite.Connection) -> None:
        await conn.execute(
            "UPDATE users SET quiet_start=23, quiet_end=8, timezone='Europe/Moscow' WHERE id=10"
        )
        await conn.execute("UPDATE users SET status='paused' WHERE id=20")
    await app.writer.submit(configure)
    await ingest(app, qid, generation, 50000, "2026-10-25T21:00:00+00:00")
    await ingest(app, qid, generation, 49000, "2026-10-25T21:00:30+00:00")

    rows = await (await app.db._conn.execute(
        "SELECT user_id, available_at FROM deliveries ORDER BY user_id"
    )).fetchall()
    assert [(int(row[0]), row[1]) for row in rows] == [
        (10, "2026-10-26T05:00:00+00:00")
    ]
    await app.dispatcher.dispatch_once("2026-10-25T21:00:30+00:00")
    assert sender.user_ids == []


@pytest.mark.asyncio
async def test_expired_delivery_is_terminal_without_external_send(policy_system):
    app, sender, qid, generation = policy_system
    await ingest(app, qid, generation, 50000, "2026-10-25T12:00:00+00:00")
    await ingest(app, qid, generation, 49000, "2026-10-25T12:00:30+00:00")
    async def age_deliveries(conn: aiosqlite.Connection) -> None:
        await conn.execute("UPDATE deliveries SET created_at='2026-10-17T10:00:00+00:00'")
        await conn.execute("UPDATE deliveries SET available_at='2026-10-17T10:00:00+00:00'")
    await app.writer.submit(age_deliveries)

    await app.dispatcher.dispatch_once("2026-10-25T12:00:30+00:00")
    statuses = await (await app.db._conn.execute(
        "SELECT DISTINCT status FROM deliveries"
    )).fetchall()
    assert {str(row[0]) for row in statuses} == {"expired"}
    assert sender.user_ids == []


@pytest.mark.asyncio
async def test_25_hour_outage_keeps_delivery_retriable(policy_system):
    app, sender, qid, generation = policy_system
    await ingest(app, qid, generation, 50000, "2026-10-25T12:00:00+00:00")
    await ingest(app, qid, generation, 49000, "2026-10-25T12:00:30+00:00")

    async def age_deliveries(conn: aiosqlite.Connection) -> None:
        await conn.execute("UPDATE deliveries SET created_at='2026-10-25T12:00:00+00:00'")
        await conn.execute("UPDATE deliveries SET available_at='2026-10-25T12:00:00+00:00'")

    await app.writer.submit(age_deliveries)
    await app.dispatcher.dispatch_once("2026-10-26T13:00:30+00:00")
    statuses = await (await app.db._conn.execute(
        "SELECT DISTINCT status FROM deliveries"
    )).fetchall()
    assert {str(row[0]) for row in statuses} == {"sent"}
    assert sender.user_ids


def test_seven_day_retention_boundary_uses_fake_clock():
    now = datetime(2026, 10, 26, 13, 0, 0, tzinfo=UTC)
    assert not is_delivery_expired(now - timedelta(days=7), now)
    assert is_delivery_expired(now - timedelta(days=7, seconds=1), now)
