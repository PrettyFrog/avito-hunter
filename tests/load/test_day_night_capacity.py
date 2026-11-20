"""Load test for the day/night schedule, shared queries, failures, and slow sink."""

from __future__ import annotations

import asyncio
import resource
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from hunter.application.capacity import CapacityAdmissionController
from hunter.application.dispatcher import DeliveryDispatcher, SendResult
from hunter.application.price_watch_worker import PriceWatchWorker
from hunter.application.slot_supervisor import SlotSupervisor
from hunter.domain.capacity import calculate_capacity_plan
from hunter.domain.observations import ObservationBatch, ObservedListing, ParseOutcome, PriceState
from hunter.domain.slots import SlotRole, SlotSignal
from hunter.storage.connection import StorageConfig, StorageConnectionManager
from hunter.storage.deliveries import ClaimedDelivery, DeliveryRepository
from hunter.storage.observation_tx import execute_observation_tx
from hunter.storage.slot_state import SlotRepository
from hunter.storage.writer import SQLiteWriterActor
from hunter.transport.price_fetch import PriceFetchAdapter, PriceFetchResult

SCHEMA = Path(__file__).resolve().parent.parent.parent / "src/hunter/core/schema.sql"


def test_day_night_schedule_and_sharing_arithmetic() -> None:
    """Verify 100 subscriptions share 40 queries and day/night capacity totals."""
    full_plan = calculate_capacity_plan(queries_count=100, slot_capacity=4, reserve_slots=2)
    assert (
        full_plan.active_slots == 25
        and full_plan.total_slots == 27
        and full_plan.total_monthly_cost_rub == 2700
    )
    assert full_plan.search_requests_per_day == 228000

    shared_plan = calculate_capacity_plan(
        queries_count=40, subscriptions_count=100, slot_capacity=4, reserve_slots=2
    )
    assert (
        shared_plan.active_slots == 10
        and shared_plan.total_slots == 12
        and shared_plan.total_monthly_cost_rub == 1200
    )
    assert shared_plan.day_polls_per_query == 2160  # 18h @ 30s
    assert shared_plan.night_polls_per_query == 120  # 6h @ 180s
    assert shared_plan.polls_per_query_day == 2280
    assert shared_plan.search_requests_per_day == 91200


@pytest.mark.asyncio
async def test_day_night_full_simulation_with_failures_and_slow_sink(tmp_path: Path) -> None:
    """Simulate shared queries across day/night boundaries, failovers, and slow delivery."""
    db_path = tmp_path / "day_night_load.db"
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.close()

    mgr = StorageConnectionManager(StorageConfig(db_path=db_path))
    writer = SQLiteWriterActor(mgr)
    await writer.start()
    delivery_repo = DeliveryRepository(mgr, writer)
    slot_repo = SlotRepository(mgr, writer)
    controller = CapacityAdmissionController(max_queries_per_slot=4)
    workers: dict[str, AsyncMock] = {}
    supervisor = SlotSupervisor(slot_repo, controller, workers)

    now_iso = datetime.now(UTC).isoformat()

    # 1. Initialize 10 active slots + 2 reserve slots via SlotRepository
    for s in range(10):
        await slot_repo.register_slot(
            f"slot_{s:02d}", f"http://10.0.0.{s + 1}:8080", role=SlotRole.ACTIVE
        )
    await slot_repo.register_slot("res_01", "http://10.0.0.101:8080", role=SlotRole.RESERVE)
    await slot_repo.register_slot("res_02", "http://10.0.0.102:8080", role=SlotRole.RESERVE)

    # Setup 40 queries, assign 4 per active slot, and map 100 subscriptions across 100 users
    async def _setup_data(c: aiosqlite.Connection) -> None:
        for u in range(1, 101):
            await c.execute(
                "INSERT INTO users (id, role, created_at) VALUES (?, 'user', ?)", (u, now_iso)
            )
            await c.execute(
                "INSERT INTO runtime (key, value) VALUES (?, ?)",
                (f"cutover:tenant:{u}", '{"mode":"v2","cutoff_sequence":0,"generation":1}'),
            )
        for q in range(40):
            qid = f"query_{q}"
            await c.execute(
                "INSERT INTO queries (id, canonical_url, bootstrap_state) VALUES (?, ?, 'ready')",
                (qid, f"https://avito.ru/q/{qid}"),
            )
        for sub in range(100):
            qid = f"query_{sub % 40}"
            await c.execute(
                "INSERT INTO subscriptions (id, user_id, query_id, max_price, enabled) "
                "VALUES (?, ?, ?, 50000, 1)",
                (f"sub_{sub}", sub + 1, qid),
            )

    await writer.submit(_setup_data)

    for q in range(40):
        sid = f"slot_{(q // 4):02d}"
        await slot_repo.assign_query(sid, f"query_{q}")

    # 2. Ingest daytime batches (30s cadence) and nighttime batches (180s cadence)
    latencies_ms: list[float] = []
    for mode, _ in (("day", 30), ("night", 180)):
        for q in range(40):
            qid = f"query_{q}"
            listings = tuple(
                ObservedListing(
                    avito_id=f"lot_{mode}_{q}_{i}",
                    title=f"Item {mode}_{q}_{i}",
                    price=15000 + (i * 500),
                    price_state=PriceState.ACTIVE,
                    url=f"https://avito.ru/item_{mode}_{q}_{i}",
                )
                for i in range(3)
            )
            batch = ObservationBatch(
                f"slot_{(q // 4):02d}", qid, now_iso, ParseOutcome.OK, listings
            )
            t0 = time.perf_counter()
            res = await writer.submit(
                lambda c, b=batch: execute_observation_tx(c, b, request_sequence=1)
            )
            latencies_ms.append((time.perf_counter() - t0) * 1000.0)
            assert res.deliveries_created > 0

    # 3. Fault injection: two slot failures claim 2 reserves; third failure exhausts reserve
    f1 = await supervisor.handle_slot_signal("slot_00", SlotSignal.HTTP_403)
    assert f1.status == "failover_complete" and f1.target_slot_id == "res_01"

    f2 = await supervisor.handle_slot_signal("slot_01", SlotSignal.CHALLENGE_DETECTED)
    assert f2.status == "failover_complete" and f2.target_slot_id == "res_02"

    f3 = await supervisor.handle_slot_signal("slot_02", SlotSignal.HTTP_429)
    assert f3.status == "degraded", (
        "3rd failure must transition to degraded capacity without crashing"
    )

    # 4. Slow Telegram sink backpressure dispatch
    slow_dispatched = 0

    async def _slow_tg(d: ClaimedDelivery) -> SendResult:
        nonlocal slow_dispatched
        await asyncio.sleep(0.001)
        slow_dispatched += 1
        return SendResult(status="sent", message_id=f"tg_{d.id}")

    virtual_time = [0.0]

    async def advance(seconds: float) -> None:
        virtual_time[0] += seconds
        await asyncio.sleep(0)

    dispatcher = DeliveryDispatcher(
        delivery_repo, senders={"telegram": _slow_tg}, batch_size=25,
        clock=lambda: virtual_time[0], sleep=advance,
    )
    dispatcher.start()
    dispatcher.wake_up()
    for _ in range(350):
        if slow_dispatched >= 100:
            break
        dispatcher.wake_up()
        await asyncio.sleep(0.02)
    await dispatcher.stop()

    # 5. Separate price watch extras (card price checks)
    fetcher = PriceFetchAdapter()
    fetcher.fetch_card_price = AsyncMock(return_value=PriceFetchResult(status="ok", price=14000))
    pworker = PriceWatchWorker(writer, fetcher=fetcher, enabled=True)

    async def _insert_jobs(c: aiosqlite.Connection) -> None:
        for j in range(10):
            await c.execute(
                "INSERT INTO price_watch_jobs (id, item_id, status, next_run_at) "
                "VALUES (?, ?, 'pending', ?)",
                (f"job_{j}", f"lot_day_0_{j % 3}", now_iso),
            )

    await writer.submit(_insert_jobs)
    processed_extras = await pworker.poll_once(now_iso)

    await writer.stop()
    await mgr.drain_and_close()

    latencies_ms.sort()
    n = len(latencies_ms)
    p50, p95, p99, max_lat = (
        latencies_ms[int(n * 0.50)],
        latencies_ms[int(n * 0.95)],
        latencies_ms[int(n * 0.99)],
        latencies_ms[-1],
    )
    rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0

    assert slow_dispatched >= 100
    assert processed_extras == 10
    assert pworker.requests_made == 10
    print(
        f"\nSIMULATION STATS: batches={n} queries=40 subs=100 search_baseline=91200 "
        f"extras={processed_extras} p50={p50:.2f}ms p95={p95:.2f}ms "
        f"p99={p99:.2f}ms max={max_lat:.2f}ms RSS={rss_mb:.1f}MB"
    )
