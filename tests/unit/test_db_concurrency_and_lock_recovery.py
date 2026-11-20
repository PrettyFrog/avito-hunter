from __future__ import annotations

import asyncio
import sqlite3
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from hunter.core.db import Database
from hunter.core.models import now_utc


@pytest.mark.asyncio
async def test_recover_leases_throttling(tmp_path: Path):
    """Test that recover_leases throttles DB executions within cooldown unless forced."""
    db = Database(tmp_path / "test_throttle.db")
    await db.connect()

    # Initial call with min_interval_sec should run
    res1 = await db.recover_leases(min_interval_sec=15.0)
    assert res1 == 0
    first_ts = db._last_lease_recovery_ts
    assert first_ts > 0.0

    # Immediate second call with min_interval_sec should be throttled
    res2 = await db.recover_leases(min_interval_sec=15.0)
    assert res2 == 0
    assert db._last_lease_recovery_ts == first_ts

    # Call with min_interval_sec=0.0 should bypass throttle and update timestamp
    res3 = await db.recover_leases(min_interval_sec=0.0)
    assert res3 == 0
    assert db._last_lease_recovery_ts >= first_ts


@pytest.mark.asyncio
async def test_concurrent_job_leasing_no_lock_errors(tmp_path: Path):
    """Test that multiple concurrent workers hammering lease_job & complete_job \
execute without SQLite lock errors."""
    db = Database(tmp_path / "test_concurrent.db")
    await db.connect()

    # Enqueue 30 test jobs
    for i in range(30):
        await db.enqueue_job(
            kind="card_fetch",
            product_id="prod_test",
            dedupe_key=f"item_{i}",
            payload={"index": i},
            priority=i,
        )

    errors = []
    processed = []

    async def worker(worker_id: int):
        for _ in range(20):
            try:
                job = await db.lease_job(kind="card_fetch", lease_duration_sec=60)
                if job:
                    processed.append(job["id"])
                    await asyncio.sleep(0.001)  # Simulate short processing
                    await db.complete_job(job["id"], {"worker": worker_id})
                else:
                    await asyncio.sleep(0.002)
            except Exception as exc:
                errors.append((worker_id, type(exc).__name__, str(exc)))

    # Run 6 workers concurrently
    await asyncio.gather(*(worker(w) for w in range(6)))

    assert len(errors) == 0, f"Encountered unexpected concurrency errors: {errors}"
    assert len(processed) == 30, f"Expected 30 jobs processed, got {len(processed)}"


@pytest.mark.asyncio
async def test_lease_job_retry_on_locked(tmp_path: Path):
    """Test that lease_job retries and succeeds if a transient database lock occurs."""
    db = Database(tmp_path / "test_retry.db")
    await db.connect()

    await db.enqueue_job(
        kind="card_fetch",
        product_id="prod_test",
        dedupe_key="item_retry",
        payload={"index": 1},
    )

    conn = await db.connect()
    original_execute = conn.execute
    attempt_count = 0

    async def flaky_execute(query, *args, **kwargs):
        nonlocal attempt_count
        if "UPDATE jobs" in str(query) and "SET status = 'leased'" in str(query):
            attempt_count += 1
            if attempt_count == 1:
                raise sqlite3.OperationalError("database is locked")
        return await original_execute(query, *args, **kwargs)

    with patch.object(conn, "execute", side_effect=flaky_execute):
        job = await db.lease_job(kind="card_fetch")
        assert job is not None
        assert job["dedupe_key"] == "item_retry"
        assert attempt_count >= 1


@pytest.mark.asyncio
async def test_lease_job_begin_immediate_retry_on_locked(tmp_path: Path) -> None:
    """Test that lease_job does not suppress BEGIN IMMEDIATE lock failure, but \
retries and succeeds."""
    db = Database(tmp_path / "test_begin_retry.db")
    await db.connect()

    await db.enqueue_job(
        kind="card_fetch",
        product_id="prod_test",
        dedupe_key="item_begin_retry",
        payload={"index": 1},
    )

    conn = await db.connect()
    original_execute = conn.execute
    begin_attempts = 0

    async def flaky_execute(query, *args, **kwargs):
        nonlocal begin_attempts
        if "BEGIN IMMEDIATE" in str(query):
            begin_attempts += 1
            if begin_attempts == 1:
                raise sqlite3.OperationalError("database is locked")
        return await original_execute(query, *args, **kwargs)

    with patch.object(conn, "execute", side_effect=flaky_execute):
        job = await db.lease_job(kind="card_fetch")
        assert job is not None
        assert job["dedupe_key"] == "item_begin_retry"
        assert begin_attempts >= 2


@pytest.mark.asyncio
async def test_concurrent_upsert_select_insert_barrier_integrity_error(tmp_path: Path) -> None:
    """Non-atomic SELECT-then-INSERT in upsert_listing raises IntegrityError \
under concurrent execution."""
    db = Database(tmp_path / "barrier_upsert.db")
    await db.connect()

    conn = await db.connect()
    orig_execute = conn.execute
    barrier = asyncio.Barrier(2)

    async def hooked_execute(sql, *args, **kwargs):
        res = await orig_execute(sql, *args, **kwargs)
        if "SELECT avito_id, price, status FROM listings" in str(sql):
            await barrier.wait()
        return res

    data = {
        "avito_id": "lot_barrier_1",
        "url": "https://avito.ru/barrier",
        "title": "Barrier Lot",
        "price": 10000,
    }

    with patch.object(conn, "execute", side_effect=hooked_execute):
        task1 = asyncio.create_task(db.upsert_listing(data))
        task2 = asyncio.create_task(db.upsert_listing(data))
        results = await asyncio.gather(task1, task2, return_exceptions=True)

    errors = [e for e in results if isinstance(e, Exception)]
    assert len(errors) == 0, f"Expected atomic upsert without race errors, but got: {errors}"

    events = await db.events("lot_barrier_1")
    first_seens = [e for e in events if e.get("kind") == "first_seen"]
    assert len(first_seens) == 1, f"Expected exactly 1 first_seen event, got: {first_seens}"


@pytest.mark.asyncio
async def test_expired_lease_completion_stale_worker_collision(tmp_path: Path) -> None:
    """A worker whose lease expired must not be able to complete a job that \
was re-leased to another worker."""
    db = Database(tmp_path / "lease_collision.db")
    await db.connect()

    await db.enqueue_job(kind="card_fetch", product_id="p1", dedupe_key="k1", payload={"data": 1})

    job1 = await db.lease_job(kind="card_fetch", lease_duration_sec=1)
    assert job1 is not None

    conn = await db.connect()
    past_ts = (now_utc() - timedelta(seconds=10)).isoformat()
    await conn.execute("UPDATE jobs SET lease_until = ? WHERE id = ?", (past_ts, job1["id"]))
    await conn.commit()

    recovered = await db.recover_leases(min_interval_sec=0.0)
    assert recovered == 1
    job2 = await db.lease_job(kind="card_fetch", lease_duration_sec=60)
    assert job2 is not None
    assert job2["id"] == job1["id"]

    await db.complete_job(job1["id"], {"result": "worker1_stale"})

    cur = await conn.execute("SELECT status, payload FROM jobs WHERE id = ?", (job1["id"],))
    row = await cur.fetchone()
    assert row[0] == "leased", (
        f"Expected job to remain leased to active worker, but status was: {row[0]}"
    )


@pytest.mark.asyncio
async def test_write_lock_serializes_cross_transaction_commit(tmp_path: Path) -> None:
    """Database helpers wait while a raw writer owns the connection lock."""
    db = Database(tmp_path / "shared_tx.db")
    await db.connect()
    inserted = asyncio.Event()
    rollback = asyncio.Event()

    async def raw_writer() -> None:
        async with db.write_lock:
            conn = await db.connect()
            await conn.execute("INSERT INTO runtime (key, value) VALUES ('task1_temp', 'draft')")
            inserted.set()
            await rollback.wait()
            await conn.rollback()

    async def api_writer() -> None:
        await inserted.wait()
        await db.set_runtime("task2_key", "done")

    raw_task = asyncio.create_task(raw_writer())
    await inserted.wait()
    api_task = asyncio.create_task(api_writer())
    try:
        await asyncio.wait_for(asyncio.shield(api_task), timeout=0.01)
    except asyncio.TimeoutError:
        pass
    rollback.set()
    await asyncio.gather(raw_task, api_task)

    val = await db.get_runtime("task1_temp")
    assert val is None, f"API helper committed another task's transaction: {val}"
    assert await db.get_runtime("task2_key") == "done"


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    reason=(
        "ISSUE:AUDIT-05 EXPIRES:2026-11-30 REVIEW:codex: raw writes outside "
        "Database.write_lock are not isolated on the shared connection"
    ),
)
async def test_unlocked_raw_transaction_is_not_isolated(tmp_path: Path) -> None:
    """Expose the unsupported raw-write path that bypasses the legacy writer lock."""
    db = Database(tmp_path / "unlocked_shared_tx.db")
    await db.connect()
    inserted = asyncio.Event()
    rollback = asyncio.Event()

    async def raw_writer() -> None:
        conn = await db.connect()
        await conn.execute("INSERT INTO runtime (key, value) VALUES ('raw_temp', 'draft')")
        inserted.set()
        await rollback.wait()
        await conn.rollback()

    async def api_writer() -> None:
        await inserted.wait()
        await db.set_runtime("api_key", "done")

    raw_task = asyncio.create_task(raw_writer())
    await inserted.wait()
    api_task = asyncio.create_task(api_writer())
    try:
        await asyncio.wait_for(asyncio.shield(api_task), timeout=0.01)
    except asyncio.TimeoutError:
        pass
    rollback.set()
    await asyncio.gather(raw_task, api_task)
    assert await db.get_runtime("raw_temp") is None


@pytest.mark.asyncio
async def test_single_writer_wal_queue_pressure_tolerance(tmp_path: Path) -> None:
    """Verify that multiple concurrent coroutines writing via single \
SQLiteWriterActor experience 0 lock errors."""
    from hunter.storage.connection import StorageConfig, StorageConnectionManager
    from hunter.storage.writer import SQLiteWriterActor

    db_p = tmp_path / "writer_pressure.db"
    con = sqlite3.connect(db_p)
    con.execute("CREATE TABLE t (id INT PRIMARY KEY, val TEXT)")
    con.close()
    mgr = StorageConnectionManager(StorageConfig(db_path=db_p))
    writer = SQLiteWriterActor(mgr)
    await writer.start()

    async def _write(i: int):
        return await writer.submit(
            lambda c: c.execute("INSERT INTO t VALUES (?, ?)", (i, f"v_{i}"))
        )

    await asyncio.gather(*(_write(i) for i in range(50)))
    await writer.stop()
    await mgr.drain_and_close()
