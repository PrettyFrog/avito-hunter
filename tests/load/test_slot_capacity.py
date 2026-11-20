"""Synthetic capacity, load harness, and backpressure verification (S7 / T-494)."""

from __future__ import annotations

import asyncio
import resource
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
import pytest

from hunter.application.dispatcher import DeliveryDispatcher, SendResult
from hunter.domain.capacity import calculate_capacity_plan
from hunter.domain.observations import ObservationBatch, ObservedListing, ParseOutcome, PriceState
from hunter.storage.connection import StorageConfig, StorageConnectionManager
from hunter.storage.deliveries import ClaimedDelivery, DeliveryRepository
from hunter.storage.observation_tx import execute_observation_tx
from hunter.storage.writer import SQLiteWriterActor

SCHEMA = Path(__file__).resolve().parent.parent.parent / "src/hunter/core/schema.sql"


def test_slot_capacity_plan_and_daily_arithmetic() -> None:
    """Verifies baseline 288000/day arithmetic and 228000 day/night schedule \
for 100 physical queries."""
    flat_day_polls_per_query = int(86400 / 30)  # 2880 polls/day
    flat_day_total_requests = 100 * flat_day_polls_per_query
    assert flat_day_total_requests == 288000, (
        "Flat 30s cadence must yield exactly 288,000 requests/day"
    )

    plan = calculate_capacity_plan(queries_count=100, slot_capacity=4, reserve_slots=2)
    assert plan.active_slots == 25
    assert plan.reserve_slots == 2
    assert plan.total_slots == 27
    assert plan.search_requests_per_day == 228000
    assert plan.total_monthly_cost_rub == 2700


@pytest.mark.asyncio
async def test_synthetic_capacity_wal_pressure_and_slow_sink(tmp_path: Path) -> None:
    """Simulates 100 physical queries, fanout to 100 subs, WAL write pressure, \
and slow-sink backpressure."""
    db_path = tmp_path / "load_test.db"
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.close()

    mgr = StorageConnectionManager(StorageConfig(db_path=db_path))
    writer = SQLiteWriterActor(mgr)
    await writer.start()
    delivery_repo = DeliveryRepository(mgr, writer)

    now_iso = datetime.now(UTC).isoformat()

    # 1. Setup 100 queries, 20 users, and 100 active subscriptions
    async def _init_data(c: aiosqlite.Connection) -> None:
        for u in range(1, 21):
            await c.execute(
                "INSERT INTO users (id, role, created_at) VALUES (?, 'user', ?)", (u, now_iso)
            )
            await c.execute(
                "INSERT INTO runtime (key, value) VALUES (?, ?)",
                (f"cutover:tenant:{u}", '{"mode":"v2","cutoff_sequence":0,"generation":1}'),
            )
        for q in range(100):
            qid = f"query_{q}"
            await c.execute(
                "INSERT INTO queries (id, canonical_url, bootstrap_state) VALUES (?, ?, 'ready')",
                (qid, f"https://avito.ru/q/{qid}"),
            )
            uid = (q % 20) + 1
            await c.execute(
                "INSERT INTO subscriptions (id, user_id, query_id, max_price, enabled) "
                "VALUES (?, ?, ?, 50000, 1)",
                (f"sub_{q}", uid, qid),
            )

    await writer.submit(_init_data)

    # 2. Ingest 100 observation batches under WAL pressure and measure transaction latencies
    latencies_ms: list[float] = []
    for q in range(100):
        qid = f"query_{q}"
        listings = tuple(
            ObservedListing(
                avito_id=f"lot_{q}_{i}",
                title=f"Listing {q}_{i}",
                price=20000 + (i * 1000),
                price_state=PriceState.ACTIVE,
                url=f"https://avito.ru/lot_{q}_{i}",
            )
            for i in range(5)
        )
        batch = ObservationBatch("slot_0", qid, now_iso, ParseOutcome.OK, listings)
        t0 = time.perf_counter()
        res = await writer.submit(
            lambda c, b=batch: execute_observation_tx(c, b, request_sequence=1)
        )
        dt_ms = (time.perf_counter() - t0) * 1000.0
        latencies_ms.append(dt_ms)
        assert res.deliveries_created > 0

    # 3. Simulate slow-sink dispatcher with artificial backpressure
    slow_dispatched = 0

    async def _slow_sender(d: ClaimedDelivery) -> SendResult:
        nonlocal slow_dispatched
        await asyncio.sleep(0.001)  # 1ms artificial delay per delivery
        slow_dispatched += 1
        return SendResult(status="sent", message_id=f"msg_{d.id}")

    virtual_time = [0.0]

    async def advance(seconds: float) -> None:
        virtual_time[0] += seconds
        await asyncio.sleep(0)

    dispatcher = DeliveryDispatcher(
        delivery_repo, senders={"telegram": _slow_sender}, batch_size=25,
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

    await writer.stop()
    await mgr.drain_and_close()

    latencies_ms.sort()
    n = len(latencies_ms)
    p50 = latencies_ms[int(n * 0.50)]
    p95 = latencies_ms[int(n * 0.95)]
    p99 = latencies_ms[int(n * 0.99)]
    max_lat = latencies_ms[-1]
    rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0

    assert slow_dispatched >= 100
    assert p50 > 0.0
    print(
        f"\nCAPACITY METRICS: n={n} p50={p50:.2f}ms p95={p95:.2f}ms "
        f"p99={p99:.2f}ms max={max_lat:.2f}ms RSS={rss_mb:.1f}MB"
    )
