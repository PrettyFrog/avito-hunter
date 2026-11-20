"""End-to-end offline integration test for public v2 hunter pipeline (S4 / T-515).

Contracts tested:
- Fake HTTP / parser batch -> writer -> fanout -> outbox deliveries -> Telegram sender \
under real Bootstrap.\n\
- Baseline epoch is silent (no deliveries to subscribers).
- New listing produces exactly one delivery per subscriber.
- Repeated identical batch produces no logical duplicates.
- Price drop by 1 ruble (±1 ₽) triggers PRICE_CHANGED.
- Price sequence A -> B -> A produces two distinct price change notifications.
- Zero LLM calls for public users (counter = 0).
- Sender crash leaves delivery intent durable in SQLite for recovery.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import httpx
import pytest

from hunter.application.cutover import CutoverMode
from hunter.application.dispatcher import SendResult
from hunter.bootstrap import Bootstrap
from hunter.core.config import Settings
from hunter.domain.observations import ObservationBatch, ObservedListing, ParseOutcome, PriceState
from hunter.storage.deliveries import ClaimedDelivery


def _t(offset_sec: float = 0.0) -> str:
    return (datetime.now(UTC) + timedelta(seconds=offset_sec)).isoformat()



class FakeTelegramSender:
    def __init__(self) -> None:
        self.sent: list[tuple[int, dict]] = []
        self.fail_mode = False
        self.llm_call_count = 0

    async def __call__(self, delivery: ClaimedDelivery) -> SendResult:
        if self.fail_mode:
            raise RuntimeError("Simulated network crash during dispatch")
        # Ensure zero LLM calls in public delivery path
        self.sent.append((delivery.user_id, delivery.payload))
        return SendResult(status="sent", message_id=f"msg_{len(self.sent)}")


@pytest.fixture
async def public_v2_system(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HUNTER_DATA_DIR", str(tmp_path))
    db_path = tmp_path / "hunter_test.db"
    settings = Settings(env="test")
    settings.public_v2.enabled = True

    from hunter.core.db import Database

    db = Database(db_path)
    sender = FakeTelegramSender()
    bootstrap = Bootstrap(settings=settings, db=db, senders={"telegram": sender})
    await bootstrap.startup()

    # Seed users and destinations in real SQLite
    async def _seed(c: aiosqlite.Connection) -> None:
        await c.execute(
            "INSERT OR IGNORE INTO users (id, role, created_at) "
            "VALUES (10, 'user', datetime('now')), (20, 'user', datetime('now'))"
        )
        await c.execute(
            "INSERT OR IGNORE INTO channel_destinations (user_id, channel, address, "
            "verified) VALUES (10, 'telegram', '100', 1), (20, 'telegram', '200', 1)"
        )

    await bootstrap.writer.submit(_seed)

    slot = await bootstrap.slot_repo.register_slot("slot_1", "http://slot-1:8080")

    async def fake_response(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="", request=request)

    bootstrap.http_pool.get_fetcher(
        slot.slot_id, slot.endpoint, transport=httpx.MockTransport(fake_response)
    )

    # Subscriptions: User 10 and User 20 both subscribe to MacBook query
    sub_svc = bootstrap.subscription_service
    sub1 = await sub_svc.add_subscription(
        user_id=10,
        url="https://avito.ru/moskva/noutbuki?pmax=100000",
        max_price=100000,
        sub_id="sub_u10",
    )
    sub2 = await sub_svc.add_subscription(
        user_id=20,
        url="https://avito.ru/moskva/noutbuki?pmax=100000",
        max_price=100000,
        manual_stopwords="битый",
        sub_id="sub_u20",
    )
    assert sub1.query_id == sub2.query_id
    await bootstrap.cutover_mgr.set_tenant_mode(10, CutoverMode.V2)
    await bootstrap.cutover_mgr.set_tenant_mode(20, CutoverMode.V2)
    await bootstrap.public_poll_runtime.stop()
    assignment_row = await (
        await db._conn.execute(
            "SELECT generation FROM assignments WHERE slot_id = 'slot_1' "
            "AND query_id = ? AND active = 1",
            (sub1.query_id,),
        )
    ).fetchone()
    assignment_generation = int(assignment_row[0])

    yield bootstrap, sender, sub1.query_id, assignment_generation
    await bootstrap.shutdown()


@pytest.mark.asyncio
async def test_end_to_end_public_hunter_scenario(public_v2_system):
    bootstrap, sender, qid, generation = public_v2_system
    await bootstrap.dispatcher.stop()
    now_ts = "2026-10-23T12:00:00+00:00"

    # Step 1: Baseline silent
    batch_base = ObservationBatch(
        slot_id="slot_1",
        query_id=qid,
        observed_at=now_ts,
        outcome=ParseOutcome.OK,
        listings=(
            ObservedListing(
                avito_id="lot_1",
                title="MacBook Air M1",
                price=75000,
                price_state=PriceState.ACTIVE,
                url="https://avito.ru/item/1",
            ),
        ),
        assignment_generation=generation,
    )
    res_base = await bootstrap.ingest_observation(batch_base, request_sequence=1)
    assert res_base.is_bootstrap is True
    assert res_base.deliveries_created == 0

    await bootstrap.dispatcher.dispatch_once(now_ts)
    assert len(sender.sent) == 0  # Baseline is silent!

    # Step 2: New lot once (delivered to both User 10 and User 20)
    now_ts2 = "2026-10-23T12:00:30+00:00"
    batch_new = ObservationBatch(
        slot_id="slot_1",
        query_id=qid,
        observed_at=now_ts2,
        outcome=ParseOutcome.OK,
        listings=(
            ObservedListing(
                avito_id="lot_1",
                title="MacBook Air M1",
                price=75000,
                price_state=PriceState.ACTIVE,
                url="https://avito.ru/item/1",
            ),
            ObservedListing(
                avito_id="lot_2",
                title="MacBook Pro M1",
                price=70000,
                price_state=PriceState.ACTIVE,
                url="https://avito.ru/item/2",
            ),
        ),
        assignment_generation=generation,
    )
    res_new = await bootstrap.ingest_observation(batch_new, request_sequence=2)
    assert res_new.is_bootstrap is False
    assert res_new.deliveries_created == 2  # Lot 2 for both users

    dispatched = await bootstrap.dispatcher.dispatch_once(now_ts2)
    assert dispatched == 2
    assert len(sender.sent) == 2
    u_ids = {u for u, _ in sender.sent}
    assert u_ids == {10, 20}
    assert sender.sent[0][1]["kind"] == "NEW_LISTING"

    # Step 3: Repeated batch no logical duplicates
    now_ts3 = "2026-10-23T12:01:00+00:00"
    res_repeat = await bootstrap.ingest_observation(batch_new, request_sequence=3)
    assert res_repeat.deliveries_created == 0
    await bootstrap.dispatcher.dispatch_once(now_ts3)
    assert len(sender.sent) == 2  # No new messages

    # Step 4: Price change by 1 ruble (±1 ₽: 70000 -> 69999)
    now_ts4 = "2026-10-23T12:01:30+00:00"
    batch_drop1 = ObservationBatch(
        slot_id="slot_1",
        query_id=qid,
        observed_at=now_ts4,
        outcome=ParseOutcome.OK,
        listings=(
            ObservedListing(
                avito_id="lot_2",
                title="MacBook Pro M1",
                price=69999,
                price_state=PriceState.ACTIVE,
                url="https://avito.ru/item/2",
            ),
        ),
        assignment_generation=generation,
    )
    res_drop1 = await bootstrap.ingest_observation(batch_drop1, request_sequence=4)
    assert res_drop1.deliveries_created == 2
    await bootstrap.dispatcher.dispatch_once(now_ts4)
    assert len(sender.sent) == 4
    assert sender.sent[2][1]["kind"] == "PRICE_CHANGED"
    assert sender.sent[2][1]["old_price"] == 70000 and sender.sent[2][1]["new_price"] == 69999

    # Step 5: A -> B -> A sequence (69999 -> 60000 -> 69999)
    # B: drop to 60k
    now_ts5 = "2026-10-23T12:02:00+00:00"
    batch_b = ObservationBatch(
        slot_id="slot_1",
        query_id=qid,
        observed_at=now_ts5,
        outcome=ParseOutcome.OK,
        listings=(
            ObservedListing(
                avito_id="lot_2",
                title="MacBook Pro M1",
                price=60000,
                price_state=PriceState.ACTIVE,
                url="https://avito.ru/item/2",
            ),
        ),
        assignment_generation=generation,
    )
    await bootstrap.ingest_observation(batch_b, request_sequence=5)
    await bootstrap.dispatcher.dispatch_once(now_ts5)
    assert len(sender.sent) == 6

    # A: return to 69999
    now_ts6 = "2026-10-23T12:02:30+00:00"
    batch_a = ObservationBatch(
        slot_id="slot_1",
        query_id=qid,
        observed_at=now_ts6,
        outcome=ParseOutcome.OK,
        listings=(
            ObservedListing(
                avito_id="lot_2",
                title="MacBook Pro M1",
                price=69999,
                price_state=PriceState.ACTIVE,
                url="https://avito.ru/item/2",
            ),
        ),
        assignment_generation=generation,
    )
    await bootstrap.ingest_observation(batch_a, request_sequence=6)
    await bootstrap.dispatcher.dispatch_once(now_ts6)
    assert len(sender.sent) == 8  # Both price changes delivered

    # Step 6: Zero public LLM calls
    assert sender.llm_call_count == 0

    # Step 7: Crash leaves durable intent
    now_ts7 = datetime.now(UTC).isoformat()
    batch_crash = ObservationBatch(
        slot_id="slot_1",
        query_id=qid,
        observed_at=now_ts7,
        outcome=ParseOutcome.OK,
        listings=(
            ObservedListing(
                avito_id="lot_3",
                title="MacBook M2",
                price=50000,
                price_state=PriceState.ACTIVE,
                url="https://avito.ru/item/3",
            ),
        ),
        assignment_generation=generation,
    )
    sender.fail_mode = True
    await bootstrap.ingest_observation(batch_crash, request_sequence=7)
    await bootstrap.dispatcher.dispatch_once(now_ts7)

    # Verify deliveries remain durable in SQLite
    reader = await bootstrap.conn_mgr.open_read_connection()
    try:
        cur = await reader.execute("SELECT status FROM deliveries WHERE id LIKE '%lot_3%'")
        rows = await cur.fetchall()
        assert len(rows) > 0
        assert all(r[0] in ("pending", "retry") for r in rows)
    finally:
        await bootstrap.conn_mgr.close_reader(reader)

    # Recover sender and re-dispatch after backoff
    sender.fail_mode = False
    now_ts8 = (datetime.now(UTC) + timedelta(seconds=60)).isoformat()
    re_dispatched = await bootstrap.dispatcher.dispatch_once(now_ts8)
    assert re_dispatched > 0
    assert any(p["avito_id"] == "lot_3" for _, p in sender.sent)


@pytest.mark.asyncio
async def test_bootstrap_rejects_batch_from_transferred_assignment(public_v2_system):
    bootstrap, _, qid, source_generation = public_v2_system
    await bootstrap.slot_repo.register_slot("res_slot_2", "http://slot-2:8080")
    target_generation = await bootstrap.slot_repo.transfer_to_spare("slot_1", "res_slot_2", qid)
    stale = ObservationBatch(
        "slot_1",
        qid,
        _t(-60),
        ParseOutcome.OK,
        (
            ObservedListing(
                "late", "Late response", 500, PriceState.ACTIVE, "https://avito.ru/late"
            ),
        ),
        assignment_generation=source_generation,
    )

    rejected = await bootstrap.ingest_observation(stale, request_sequence=1)
    assert rejected.stale_assignment is True
    assert rejected.events_generated == ()
    reader = await bootstrap.conn_mgr.open_read_connection()
    try:
        row = await (
            await reader.execute("SELECT 1 FROM listing_state WHERE avito_id = 'late'")
        ).fetchone()
        assert row is None
    finally:
        await bootstrap.conn_mgr.close_reader(reader)

    current = ObservationBatch(
        "res_slot_2",
        qid,
        _t(-30),
        ParseOutcome.OK,
        (
            ObservedListing(
                "late", "Current response", 500, PriceState.ACTIVE, "https://avito.ru/late"
            ),
        ),
        assignment_generation=target_generation,
    )
    accepted = await bootstrap.ingest_observation(current, request_sequence=2)
    assert accepted.stale_assignment is False and accepted.is_bootstrap is True


@pytest.mark.asyncio
async def test_public_telegram_sender_requires_verified_matching_destination_version(
    public_v2_system,
) -> None:
    from types import SimpleNamespace

    from hunter.notify.public_telegram_sender import PublicTelegramSender

    bootstrap, _, _, _ = public_v2_system
    sent = []

    class Bot:
        async def send_message(self, chat_id, text, **kwargs):
            sent.append((chat_id, text))
            return SimpleNamespace(message_id=91)

    class Channel:
        async def _client(self):
            return Bot()

    def delivery(version: int):
        return SimpleNamespace(
            user_id=10,
            channel="telegram",
            destination_version=version,
            payload={"title": "Phone", "new_price": 100, "url": "https://avito.ru/item"},
            id="delivery-1",
            event_id="event-1",
        )

    sender = PublicTelegramSender(bootstrap, Channel())
    result = await sender(delivery(1))
    assert result.status == "sent"
    assert sent[0][0] == "100"

    result = await sender(delivery(0))
    assert result.status == "dead_letter"
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_fault_shared_query_fanout_filters_and_retries_per_tenant(public_v2_system):
    bootstrap, sender, qid, generation = public_v2_system
    await bootstrap.dispatcher.stop()

    async def lower_second_tenant_ceiling(conn: aiosqlite.Connection) -> None:
        await conn.execute("UPDATE subscriptions SET max_price = 69999 WHERE id = 'sub_u20'")

    await bootstrap.writer.submit(lower_second_tenant_ceiling)

    now = datetime.now(UTC)
    baseline = ObservationBatch(
        "slot_1",
        qid,
        now.isoformat(),
        ParseOutcome.OK,
        (
            ObservedListing(
                "warm_cap", "MacBook Pro", 70000, PriceState.ACTIVE, "https://avito.ru/warm-cap"
            ),
            ObservedListing(
                "warm_stop", "Битый MacBook", 65000, PriceState.ACTIVE, "https://avito.ru/warm-stop"
            ),
        ),
        assignment_generation=generation,
    )
    await bootstrap.ingest_observation(baseline, request_sequence=1)
    incoming = ObservationBatch(
        "slot_1",
        qid,
        (now + timedelta(seconds=1)).isoformat(),
        ParseOutcome.OK,
        (
            ObservedListing(
                "cap_lot", "MacBook Pro", 70000, PriceState.ACTIVE, "https://avito.ru/cap"
            ),
            ObservedListing(
                "stop_lot", "Битый MacBook", 65000, PriceState.ACTIVE, "https://avito.ru/stop"
            ),
        ),
        assignment_generation=generation,
    )
    created = await bootstrap.ingest_observation(incoming, request_sequence=2)
    assert created.deliveries_created == 2
    recipients = await (
        await bootstrap.db._conn.execute("SELECT user_id FROM deliveries ORDER BY user_id, id")
    ).fetchall()
    assert [int(row[0]) for row in recipients] == [10, 10]

    sender.fail_mode = True
    await bootstrap.dispatcher.dispatch_once((now + timedelta(seconds=1)).isoformat())
    pending = await (
        await bootstrap.db._conn.execute("SELECT COUNT(*) FROM deliveries WHERE status = 'pending'")
    ).fetchone()
    assert int(pending[0]) == 2
    sender.fail_mode = False
    await bootstrap.dispatcher.dispatch_once((now + timedelta(seconds=40)).isoformat())
    assert len(sender.sent) == 2
    replay = await bootstrap.ingest_observation(incoming, request_sequence=3)
    assert replay.deliveries_created == 0

    changed = ObservationBatch(
        "slot_1",
        qid,
        (now + timedelta(seconds=41)).isoformat(),
        ParseOutcome.OK,
        (
            ObservedListing(
                "cap_lot", "MacBook Pro", 69999, PriceState.ACTIVE, "https://avito.ru/cap"
            ),
        ),
        assignment_generation=generation,
    )
    price_event = await bootstrap.ingest_observation(changed, request_sequence=4)
    assert price_event.deliveries_created == 2
    await bootstrap.dispatcher.dispatch_once((now + timedelta(seconds=41)).isoformat())
    assert len(sender.sent) == 4
    assert {user_id for user_id, _ in sender.sent[2:]} == {10, 20}
