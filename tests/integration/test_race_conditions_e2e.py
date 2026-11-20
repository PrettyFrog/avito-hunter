"""End-to-end race condition stress tests for T-B092: Concurrency & Races (RB-2).

Verifies resilience under real concurrent conditions:
1. Search parameter edit during active observation ingest (no half-states, CAS consistency).
2. Multi-tenant parallel search mutations (serialized by SQLiteWriterActor without corruption).
3. Concurrent observation ingest and delivery dispatching (zero lost notifications, no clobbered leases).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import aiosqlite
import pytest

from hunter.application.cutover import CutoverMode
from hunter.application.dispatcher import SendResult
from hunter.bootstrap import Bootstrap
from hunter.core.config import Settings
from hunter.core.db import Database
from hunter.core.models import ObservedListing, PriceState
from hunter.domain.observations import ObservationBatch, ParseOutcome
from hunter.storage.observation_tx import execute_observation_tx
from hunter.storage.subscriptions import SubscriptionActor

pytest_plugins = ("tests.integration.test_public_hunter",)


async def _setup_tenant(app: Bootstrap, user_id: int, chat_id: int, sub_id: str, url: str, price: int):
    async def init_user(conn: aiosqlite.Connection) -> None:
        await conn.execute(
            "INSERT OR IGNORE INTO users (id, telegram_user_id, chat_id, role, status, created_at) "
            "VALUES (?, ?, ?, 'user', 'active', datetime('now'))",
            (user_id, user_id, chat_id),
        )
    await app.writer.submit(init_user)
    await app.cutover_mgr.set_tenant_mode(user_id, CutoverMode.V2)
    return await app.subscription_service.add_subscription(
        user_id, url, price, sub_id=sub_id, name=f"Search_{user_id}"
    )


@pytest.mark.asyncio
async def test_concurrent_search_edit_during_active_observation_tx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HUNTER_DATA_DIR", str(tmp_path))
    settings = Settings(env="test")
    settings.public_v2.enabled = True
    app = Bootstrap(settings, db=Database(tmp_path / "race_edit.db"), senders={"telegram": lambda d: SendResult("sent")})
    await app.startup()
    await app.dispatcher.stop()

    async def init_slot(conn: aiosqlite.Connection) -> None:
        await conn.execute(
            "INSERT INTO slots (id, endpoint, cookie_namespace, health_state, active_generation) "
            "VALUES ('slot_race', 'http://127.0.0.1:8000', 'default', 'healthy', 1)"
        )
    await app.writer.submit(init_slot)

    sub = await _setup_tenant(app, 1001, 1001, "sub_race_edit", "https://www.avito.ru/item?pmax=50000", 50000)
    actor = SubscriptionActor(1001)
    qid = sub.query_id

    # Baseline quiet seeding
    b0 = ObservationBatch(
        slot_id="slot_race", query_id=qid, observed_at="2026-10-30T10:00:00+00:00",
        outcome=ParseOutcome.OK, listings=(ObservedListing("b0", "Item 0", 40000, PriceState.ACTIVE, "http://a/0"),)
    )
    await app.writer.submit(lambda c: execute_observation_tx(c, b0, request_sequence=1))

    async def ingest_task() -> None:
        for i in range(2, 7):
            batch = ObservationBatch(
                slot_id="slot_race", query_id=qid, observed_at=f"2026-10-30T10:0{i}:00+00:00",
                outcome=ParseOutcome.OK,
                listings=(ObservedListing(f"item_{i}", f"Laptop {i}", 45000 + i * 1000, PriceState.ACTIVE, f"http://a/{i}"),)
            )
            await app.writer.submit(lambda c, b=batch, seq=i: execute_observation_tx(c, b, request_sequence=seq))
            await asyncio.sleep(0.01)

    async def edit_task() -> None:
        for p in (55000, 60000, 65000):
            current = await app.subscription_repo.get(actor, "sub_race_edit")
            await app.subscription_repo.update(actor, "sub_race_edit", expected_version=current.version, max_price=p)
            await asyncio.sleep(0.015)

    try:
        await asyncio.gather(ingest_task(), edit_task())
        final_sub = await app.subscription_repo.get(actor, "sub_race_edit")
        assert final_sub.max_price == 65000
        assert final_sub.version >= 4
    finally:
        await app.shutdown()


@pytest.mark.asyncio
async def test_multi_tenant_parallel_search_mutations_stress(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HUNTER_DATA_DIR", str(tmp_path))
    settings = Settings(env="test")
    settings.public_v2.enabled = True
    app = Bootstrap(settings, db=Database(tmp_path / "race_mutations.db"), senders={"telegram": lambda d: SendResult("sent")})
    await app.startup()
    await app.dispatcher.stop()

    async def init_slot(conn: aiosqlite.Connection) -> None:
        await conn.execute(
            "INSERT INTO slots (id, endpoint, cookie_namespace, health_state, active_generation) "
            "VALUES ('slot_race_mut', 'http://127.0.0.1:8000', 'default', 'healthy', 1)"
        )
    await app.writer.submit(init_slot)

    # 4 distinct tenants
    tenants = [2001, 2002, 2003, 2004]
    for uid in tenants:
        await _setup_tenant(app, uid, uid, f"sub_{uid}", f"https://www.avito.ru/search{uid}", 30000)

    async def tenant_worker(uid: int) -> None:
        actor = SubscriptionActor(uid)
        sub_id = f"sub_{uid}"
        for step in range(1, 5):
            cur = await app.subscription_repo.get(actor, sub_id)
            new_price = 30000 + step * 5000
            new_words = f"stop_{step}"
            await app.subscription_repo.update(
                actor, sub_id, expected_version=cur.version, max_price=new_price, words=new_words
            )

    try:
        await asyncio.gather(*(tenant_worker(uid) for uid in tenants))
        for uid in tenants:
            actor = SubscriptionActor(uid)
            sub = await app.subscription_repo.get(actor, f"sub_{uid}")
            assert sub.version == 5
            assert sub.max_price == 50000
            assert sub.manual_stopwords == "stop_4"
    finally:
        await app.shutdown()


@pytest.mark.asyncio
async def test_concurrent_ingest_and_dispatch_interleaving(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HUNTER_DATA_DIR", str(tmp_path))
    settings = Settings(env="test")
    settings.public_v2.enabled = True

    sent_count = 0

    async def counting_sender(delivery) -> SendResult:
        nonlocal sent_count
        sent_count += 1
        return SendResult("sent", f"msg_{sent_count}")

    app = Bootstrap(settings, db=Database(tmp_path / "race_dispatch.db"), senders={"telegram": counting_sender})
    await app.startup()
    await app.dispatcher.stop()

    async def init_slot(conn: aiosqlite.Connection) -> None:
        await conn.execute(
            "INSERT INTO slots (id, endpoint, cookie_namespace, health_state, active_generation) "
            "VALUES ('slot_race_disp', 'http://127.0.0.1:8000', 'default', 'healthy', 1)"
        )
    await app.writer.submit(init_slot)

    sub = await _setup_tenant(app, 3001, 3001, "sub_disp_race", "https://www.avito.ru/disp?pmax=100000", 100000)
    qid = sub.query_id

    # Baseline
    b0 = ObservationBatch(
        slot_id="slot_race_disp", query_id=qid, observed_at="2026-10-30T10:00:00+00:00",
        outcome=ParseOutcome.OK, listings=(ObservedListing("b0", "Base", 50000, PriceState.ACTIVE, "http://a/0"),)
    )
    await app.writer.submit(lambda c: execute_observation_tx(c, b0, request_sequence=1))

    # Concurrently ingest 5 distinct lots while periodically dispatching
    async def producer() -> None:
        for i in range(2, 7):
            batch = ObservationBatch(
                slot_id="slot_race_disp", query_id=qid, observed_at=f"2026-10-30T10:0{i}:00+00:00",
                outcome=ParseOutcome.OK,
                listings=(
                    ObservedListing("b0", "Base", 50000, PriceState.ACTIVE, "http://a/0"),
                    ObservedListing(f"deal_{i}", f"Deal {i}", 60000, PriceState.ACTIVE, f"http://a/{i}"),
                )
            )
            await app.writer.submit(lambda c, b=batch, seq=i: execute_observation_tx(c, b, request_sequence=seq))
            await asyncio.sleep(0.01)

    async def consumer() -> None:
        for i in range(2, 10):
            # Make due immediately
            await app.writer.submit(
                lambda c: c.execute("UPDATE deliveries SET available_at = '2020-01-01T00:00:00+00:00' WHERE status='pending'")
            )
            await app.dispatcher.dispatch_once(f"2026-10-30T10:0{i}:00+00:00")
            await asyncio.sleep(0.01)

    try:
        await asyncio.gather(producer(), consumer())
        # Final sweep
        await app.writer.submit(
            lambda c: c.execute("UPDATE deliveries SET available_at = '2020-01-01T00:00:00+00:00' WHERE status='pending'")
        )
        await app.dispatcher.dispatch_once("2026-10-30T10:15:00+00:00")
        assert sent_count == 5, f"Expected exactly 5 alerts dispatched, got {sent_count}"
    finally:
        await app.shutdown()
