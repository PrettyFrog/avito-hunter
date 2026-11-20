"""Hermetic release-capacity simulation for shared queries, SQLite, and slot failover."""

from __future__ import annotations

import asyncio
import json
import resource
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import aiosqlite
import pytest

from hunter.application.capacity import CapacityAdmissionController
from hunter.application.dispatcher import DeliveryDispatcher, SendResult
from hunter.application.slot_supervisor import SlotSupervisor
from hunter.domain.capacity import calculate_capacity_plan
from hunter.domain.observations import ObservationBatch, ObservedListing, ParseOutcome, PriceState
from hunter.domain.poll_policy import PollPolicy
from hunter.domain.slots import SlotRole, SlotSignal
from hunter.storage.connection import StorageConfig, StorageConnectionManager
from hunter.storage.deliveries import DeliveryRepository
from hunter.storage.observation_tx import execute_observation_tx
from hunter.storage.slot_state import SlotRepository
from hunter.storage.writer import SQLiteWriterActor

SCHEMA = Path(__file__).resolve().parents[2] / "src/hunter/core/schema.sql"
NOW = "2026-10-27T10:00:00+00:00"


def _initialize_database(db_path: Path) -> None:
    connection = sqlite3.connect(db_path)
    connection.executescript(SCHEMA.read_text(encoding="utf-8"))
    connection.close()


async def _start_slot_store(db_path: Path, active_count: int, reserve_count: int):
    _initialize_database(db_path)
    manager = StorageConnectionManager(StorageConfig(db_path=db_path))
    writer = SQLiteWriterActor(manager)
    await writer.start()
    repository = SlotRepository(manager, writer)
    for index in range(active_count):
        await repository.register_slot(
            f"active-{index:02d}", f"http://127.0.0.1:{8000 + index}", role=SlotRole.ACTIVE
        )
    for index in range(reserve_count):
        await repository.register_slot(
            f"reserve-{index + 1:02d}", f"http://127.0.0.1:{8100 + index}",
            role=SlotRole.RESERVE,
        )
    return manager, writer, repository


async def _seed_tenants_and_queries(writer: SQLiteWriterActor) -> None:
    async def seed(connection: aiosqlite.Connection) -> None:
        for query_number in range(100):
            await connection.execute(
                "INSERT INTO queries (id, canonical_url, bootstrap_state) "
                "VALUES (?, ?, 'ready')",
                (f"query-{query_number:03d}", f"https://avito.ru/query-{query_number:03d}"),
            )
        for user_id in range(1, 301):
            await connection.execute(
                "INSERT INTO users (id, role, created_at) VALUES (?, 'user', ?)",
                (user_id, NOW),
            )
            await connection.execute(
                "INSERT INTO runtime (key, value) VALUES (?, ?)",
                (f"cutover:tenant:{user_id}", '{"mode":"v2","generation":1}'),
            )
            query_number = (user_id - 1) // 3
            ceiling = (15000, 20000, 30000)[(user_id - 1) % 3]
            await connection.execute(
                "INSERT INTO subscriptions (id, user_id, query_id, max_price, enabled) "
                "VALUES (?, ?, ?, ?, 1)",
                (f"sub-{user_id:03d}", user_id, f"query-{query_number:03d}", ceiling),
            )
    await writer.submit(seed)


async def _assign_queries(
    repository: SlotRepository, query_count: int
) -> dict[str, tuple[str, int]]:
    assignments: dict[str, tuple[str, int]] = {}
    for query_number in range(query_count):
        query_id = f"query-{query_number:03d}"
        slot_id = f"active-{query_number // 4:02d}"
        generation = await repository.assign_query(slot_id, query_id)
        assignments[query_id] = (slot_id, generation)
    return assignments


def _make_batch(query_id: str, assignment: tuple[str, int]) -> ObservationBatch:
    slot_id, generation = assignment
    listing = ObservedListing(
        f"lot-{query_id}", "Synthetic headset", 20000, PriceState.ACTIVE,
        f"https://avito.ru/item/{query_id}",
    )
    return ObservationBatch(
        slot_id, query_id, NOW, ParseOutcome.OK, (listing,), assignment_generation=generation
    )


async def _ingest_shared_queries(
    writer: SQLiteWriterActor, assignments: dict[str, tuple[str, int]]
):
    transaction_ms: list[float] = []

    async def ingest(query_id: str, assignment: tuple[str, int]):
        batch = _make_batch(query_id, assignment)
        started = time.perf_counter()

        async def transaction(connection: aiosqlite.Connection):
            transaction_started = time.perf_counter()
            result = await execute_observation_tx(
                connection, batch, request_sequence=1, enforce_assignment=True
            )
            transaction_ms.append((time.perf_counter() - transaction_started) * 1000.0)
            return result

        result = await writer.submit(
            transaction
        )
        return (time.perf_counter() - started) * 1000.0, result

    measurements = []
    queue_depth = 0
    for slot_position in range(4):
        wave = [
            asyncio.create_task(ingest(qid, assignment))
            for qid, assignment in assignments.items()
            if int(qid.rsplit("-", 1)[1]) % 4 == slot_position
        ]
        await asyncio.sleep(0)
        queue_depth = max(queue_depth, writer._queue.qsize())
        measurements.extend(await asyncio.gather(*wave))
    return (
        [item[0] for item in measurements], [item[1] for item in measurements],
        queue_depth, transaction_ms,
    )


async def _drain_slow_sink(repository: DeliveryRepository, now_iso: str, expected: int) -> int:
    delivered = 0
    virtual_time = [0.0]

    async def send(_delivery) -> SendResult:
        nonlocal delivered
        await asyncio.sleep(0.001)
        delivered += 1
        return SendResult(status="sent", message_id=f"fake-{delivered}")

    async def advance(seconds: float) -> None:
        virtual_time[0] += seconds
        await asyncio.sleep(0)

    dispatcher = DeliveryDispatcher(
        repository, {"telegram": send}, batch_size=40,
        clock=lambda: virtual_time[0], sleep=advance,
    )
    for _ in range((expected // 40) + 2):
        if await dispatcher.dispatch_once(now_iso) == 0:
            break
    return delivered


async def _assert_price_filtered_recipients(
    manager: StorageConnectionManager, expected: int
) -> None:
    reader = await manager.open_read_connection()
    try:
        cursor = await reader.execute(
            "SELECT count(*) FROM deliveries WHERE status = 'pending'"
        )
        assert (await cursor.fetchone())[0] == expected
        cursor = await reader.execute(
            "SELECT re.user_id FROM recipient_events re JOIN events e ON e.id = re.event_id "
            "WHERE e.origin = 'query-000' ORDER BY re.user_id"
        )
        assert [row[0] for row in await cursor.fetchall()] == [2, 3]
    finally:
        await manager.close_reader(reader)


async def _fail_two_full_slots(repository: SlotRepository) -> tuple[str, str]:
    controller = CapacityAdmissionController(max_queries_per_slot=4)
    supervisor = SlotSupervisor(repository, controller)
    first = await supervisor.handle_slot_signal("active-00", SlotSignal.HTTP_403)
    second = await supervisor.handle_slot_signal("active-01", SlotSignal.CHALLENGE_DETECTED)
    assert first.status == second.status == "failover_complete"
    return first.target_slot_id or "", second.target_slot_id or ""


def _percentile(samples: list[float], fraction: float) -> float:
    ordered = sorted(samples)
    return ordered[min(int((len(ordered) - 1) * fraction), len(ordered) - 1)]


def _write_report(path: Path, report: dict) -> None:
    path.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    assert json.loads(path.read_text(encoding="utf-8")) == report


def test_capacity_plan_and_day_night_boundaries() -> None:
    plan = calculate_capacity_plan(100, subscriptions_count=300, slot_capacity=4, reserve_slots=2)
    assert (plan.active_slots, plan.reserve_slots, plan.total_slots) == (25, 2, 27)
    assert (plan.day_polls_per_query, plan.night_polls_per_query) == (2160, 120)
    assert plan.search_requests_per_day == 228000
    policy = PollPolicy()
    msk = ZoneInfo("Europe/Moscow")
    assert policy.get_cycle_interval(datetime(2026, 10, 27, 0, 59, 59, tzinfo=msk)) == 30
    assert policy.get_cycle_interval(datetime(2026, 10, 27, 1, 0, 0, tzinfo=msk)) == 180
    assert policy.get_cycle_interval(datetime(2026, 10, 27, 7, 0, 0, tzinfo=msk)) == 30


@pytest.mark.asyncio
async def test_release_capacity_price_sharing_writer_and_delivery(tmp_path: Path) -> None:
    manager, writer, slots = await _start_slot_store(tmp_path / "release.db", 25, 2)
    try:
        await _seed_tenants_and_queries(writer)
        assignments = await _assign_queries(slots, 100)
        latencies, outcomes, queue_depth, transaction_ms = await _ingest_shared_queries(
            writer, assignments
        )
        expected_deliveries = 200  # two of three personal ceilings admit each $20k fake lot
        assert len(outcomes) == 100
        assert all(result.deliveries_created == 2 for result in outcomes)

        await _assert_price_filtered_recipients(manager, expected_deliveries)

        delivery_repository = DeliveryRepository(manager, writer)
        delivered = await _drain_slow_sink(
            delivery_repository, NOW, expected_deliveries
        )
        reserve_targets = await _fail_two_full_slots(slots)
        assert delivered == expected_deliveries
        assert reserve_targets == ("reserve-01", "reserve-02")

        def _get_process_peak_rss_mb() -> float:
            try:
                with open("/proc/self/status", encoding="utf-8") as f:
                    for line in f:
                        if line.startswith("VmHWM:"):
                            parts = line.split()
                            if len(parts) >= 2 and parts[1].isdigit():
                                return float(parts[1]) / 1024.0
            except Exception:
                pass
            return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0

        rss_mb = _get_process_peak_rss_mb()
        report = {
            "environment": "synthetic-local-runner",
            "physical_queries": 100,
            "logical_subscriptions": 300,
            "active_slots": 25,
            "reserve_slots": 2,
            "search_requests_per_day": 228000,
            "price_sharing_recall": delivered / expected_deliveries,
            "deliveries_expected": expected_deliveries,
            "deliveries_accepted_by_fake_sink": delivered,
            "reserve_failovers": list(reserve_targets),
            "writer_p50_ms": round(_percentile(latencies, 0.50), 3),
            "writer_p95_ms": round(_percentile(latencies, 0.95), 3),
            "writer_p99_ms": round(_percentile(latencies, 0.99), 3),
            "writer_transaction_p95_ms": round(_percentile(transaction_ms, 0.95), 3),
            "writer_queue_depth_sample": queue_depth,
            "peak_rss_mb": round(rss_mb, 2),
            "limits": {"writer_p95_ms": 50, "peak_rss_mb": 4096},
        }
        report["writer_p95_within_limit"] = (
            report["writer_p95_ms"] <= report["limits"]["writer_p95_ms"]
        )
        _write_report(tmp_path / "release_capacity_report.json", report)
        print(f"CAPACITY_REPORT={json.dumps(report, sort_keys=True)}")
        assert report["price_sharing_recall"] == 1.0
        assert report["writer_p95_ms"] > 0
        assert report["peak_rss_mb"] <= report["limits"]["peak_rss_mb"]
        assert report["writer_queue_depth_sample"] > 0
    finally:
        await writer.stop()
        await manager.drain_and_close()


@pytest.mark.asyncio
async def test_fault_two_full_slots_fail_over_to_two_reserves(tmp_path: Path) -> None:
    manager, writer, repository = await _start_slot_store(tmp_path / "fault.db", 3, 2)
    try:
        for slot_index in range(3):
            for query_index in range(4):
                await repository.assign_query(
                    f"active-{slot_index:02d}", f"fault-query-{slot_index}-{query_index}"
                )
        targets = await _fail_two_full_slots(repository)
        assert targets == ("reserve-01", "reserve-02")
        first = await repository.get_slot(targets[0])
        second = await repository.get_slot(targets[1])
        assert first is not None and first.capacity.current_load == 4
        assert second is not None and second.capacity.current_load == 4
    finally:
        await writer.stop()
        await manager.drain_and_close()
