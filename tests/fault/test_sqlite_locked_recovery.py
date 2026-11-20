"""Fault injection and crash-recovery tests for T-B093 (RB-2).

Verifies resilience against:
1. SQLite locked / transaction rollback recovery (Single-Writer actor remains healthy).
2. Mid-transaction failures during observation ingest (zero half-states left in DB).
3. Duplicate and out-of-order Avito responses (stale sequence detection & idempotency).
"""

from __future__ import annotations

from pathlib import Path
import aiosqlite
import pytest
import sqlite3

from hunter.bootstrap import Bootstrap
from hunter.core.config import Settings
from hunter.core.db import Database
from hunter.core.models import ObservedListing, PriceState
from hunter.domain.observations import ObservationBatch, ParseOutcome
from hunter.storage.connection import StorageConfig, StorageConnectionManager
from hunter.storage.observation_tx import execute_observation_tx
from hunter.storage.writer import SQLiteWriterActor

pytest_plugins = ("tests.integration.test_public_hunter",)


@pytest.mark.asyncio
async def test_sqlite_locked_and_rollback_recovery(tmp_path: Path) -> None:
    db_path = tmp_path / "locked_recovery.db"
    conn_mgr = StorageConnectionManager(StorageConfig(db_path=db_path))
    writer = SQLiteWriterActor(conn_mgr)
    await writer.start()

    async def init_schema(conn: aiosqlite.Connection) -> None:
        await conn.execute("CREATE TABLE test_data (id INT PRIMARY KEY, val TEXT)")

    await writer.submit(init_schema)

    # 1. Successful insert
    await writer.submit(lambda c: c.execute("INSERT INTO test_data VALUES (1, 'initial')"))

    # 2. Transaction that fails mid-way (simulating integrity / lock error)
    async def failing_tx(conn: aiosqlite.Connection) -> None:
        await conn.execute("INSERT INTO test_data VALUES (2, 'second')")
        # Trigger duplicate primary key error
        await conn.execute("INSERT INTO test_data VALUES (1, 'conflict')")

    with pytest.raises(sqlite3.IntegrityError):
        await writer.submit(failing_tx)

    # 3. Verify rollback: record 2 must NOT exist
    async def check_rows(conn: aiosqlite.Connection) -> list[tuple]:
        cur = await conn.execute("SELECT id, val FROM test_data ORDER BY id")
        return [tuple(r) for r in await cur.fetchall()]

    rows = await writer.submit(check_rows)
    assert rows == [(1, "initial")], "Failing transaction must be fully rolled back"

    # 4. Verify writer actor is still healthy and accepts subsequent writes
    await writer.submit(lambda c: c.execute("INSERT INTO test_data VALUES (3, 'recovered')"))
    rows_after = await writer.submit(check_rows)
    assert rows_after == [(1, "initial"), (3, "recovered")]

    await writer.stop()
    await conn_mgr.drain_and_close()


@pytest.mark.asyncio
async def test_fault_injection_during_observation_tx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HUNTER_DATA_DIR", str(tmp_path))
    settings = Settings(env="test")
    settings.public_v2.enabled = True
    app = Bootstrap(settings, db=Database(tmp_path / "fault_obs.db"), senders={"telegram": lambda d: None})
    await app.startup()
    await app.dispatcher.stop()

    async def init_slot(conn: aiosqlite.Connection) -> None:
        await conn.execute(
            "INSERT INTO slots (id, endpoint, cookie_namespace, health_state, active_generation) "
            "VALUES ('slot_fault', 'http://127.0.0.1:8000', 'default', 'healthy', 1)"
        )
        await conn.execute(
            "INSERT INTO users (id, telegram_user_id, chat_id, role, status, created_at) "
            "VALUES (4001, 4001, 4001, 'user', 'active', datetime('now'))"
        )
    await app.writer.submit(init_slot)

    sub = await app.subscription_service.add_subscription(
        4001, "https://www.avito.ru/fault?pmax=50000", 50000, sub_id="sub_fault"
    )
    qid = sub.query_id

    # Simulated batch
    batch = ObservationBatch(
        slot_id="slot_fault", query_id=qid, observed_at="2026-10-30T10:00:00+00:00",
        outcome=ParseOutcome.OK,
        listings=(ObservedListing("fl_1", "Fault Lot", 40000, PriceState.ACTIVE, "http://a/fl1"),)
    )

    # Ingest with injected failure halfway
    async def broken_observation_tx(conn: aiosqlite.Connection) -> None:
        await execute_observation_tx(conn, batch, request_sequence=1)
        # Injected fault: raise simulated disk/network crash before commit
        raise OSError("Simulated disk write failure mid-transaction")

    with pytest.raises(OSError, match="Simulated disk write failure"):
        await app.writer.submit(broken_observation_tx)

    # Verify zero residual data
    async def verify_clean(conn: aiosqlite.Connection) -> tuple[int, int]:
        c1 = await (await conn.execute("SELECT count(*) FROM events")).fetchone()
        c2 = await (await conn.execute("SELECT count(*) FROM deliveries")).fetchone()
        return (c1[0], c2[0])

    counts = await app.writer.submit(verify_clean)
    assert counts == (0, 0), "Aborted observation transaction must leave zero events and deliveries"

    # Now ingest cleanly
    res = await app.writer.submit(lambda c: execute_observation_tx(c, batch, request_sequence=1))
    assert res.is_bootstrap is True
    assert res.deliveries_created == 0

    await app.shutdown()


@pytest.mark.asyncio
async def test_idempotent_duplicate_and_out_of_order_recovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HUNTER_DATA_DIR", str(tmp_path))
    settings = Settings(env="test")
    settings.public_v2.enabled = True
    app = Bootstrap(settings, db=Database(tmp_path / "idemp_obs.db"), senders={"telegram": lambda d: None})
    await app.startup()
    await app.dispatcher.stop()

    async def init_slot(conn: aiosqlite.Connection) -> None:
        await conn.execute(
            "INSERT INTO slots (id, endpoint, cookie_namespace, health_state, active_generation) "
            "VALUES ('slot_idemp', 'http://127.0.0.1:8000', 'default', 'healthy', 1)"
        )
    await app.writer.submit(init_slot)

    sub = await app.subscription_service.add_subscription(
        5001, "https://www.avito.ru/idemp?pmax=70000", 70000, sub_id="sub_idemp"
    )
    qid = sub.query_id

    # 1. Ingest baseline sequence 10
    b10 = ObservationBatch(
        slot_id="slot_idemp", query_id=qid, observed_at="2026-10-30T10:00:00+00:00",
        outcome=ParseOutcome.OK,
        listings=(ObservedListing("l10", "Lot 10", 60000, PriceState.ACTIVE, "http://a/10"),)
    )
    res10 = await app.writer.submit(lambda c: execute_observation_tx(c, b10, request_sequence=10))
    assert res10.stale_sequence is False

    # 2. Out-of-order stale sequence 5 arrives for already-seen lot l10 (e.g. delayed packet)
    b5 = ObservationBatch(
        slot_id="slot_idemp", query_id=qid, observed_at="2026-10-30T09:59:00+00:00",
        outcome=ParseOutcome.OK,
        listings=(ObservedListing("l10", "Old Lot 10", 60000, PriceState.ACTIVE, "http://a/10"),)
    )
    res5 = await app.writer.submit(lambda c: execute_observation_tx(c, b5, request_sequence=5))
    assert res5.stale_sequence is True, "Stale sequence must be rejected"
    assert res5.deliveries_created == 0

    # 3. Duplicate sequence 10 arrives (idempotent, 0 new events, 0 deliveries)
    res10_dup = await app.writer.submit(lambda c: execute_observation_tx(c, b10, request_sequence=10))
    assert len(res10_dup.events_generated) == 0, "Duplicate sequence must generate 0 new events"
    assert res10_dup.deliveries_created == 0, "Duplicate sequence must generate 0 deliveries"

    # 4. Next valid sequence 11 arrives with new lot
    b11 = ObservationBatch(
        slot_id="slot_idemp", query_id=qid, observed_at="2026-10-30T10:05:00+00:00",
        outcome=ParseOutcome.OK,
        listings=(
            ObservedListing("l10", "Lot 10", 60000, PriceState.ACTIVE, "http://a/10"),
            ObservedListing("l11", "Fresh Lot 11", 62000, PriceState.ACTIVE, "http://a/11"),
        )
    )
    res11 = await app.writer.submit(lambda c: execute_observation_tx(c, b11, request_sequence=11))
    assert res11.stale_sequence is False
    assert res11.observed_count == 2

    await app.shutdown()
