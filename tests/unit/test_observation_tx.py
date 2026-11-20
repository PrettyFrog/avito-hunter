"""Unit tests for atomic observation transaction (T-464)."""

import sqlite3
from pathlib import Path

import aiosqlite
import pytest

from hunter.domain.events import EventType
from hunter.domain.observations import (
    ObservationBatch,
    ObservedListing,
    ParseOutcome,
    PriceState,
)
from hunter.storage.connection import StorageConfig, StorageConnectionManager
from hunter.storage.observation_tx import execute_observation_tx
from hunter.storage.writer import SQLiteWriterActor


@pytest.fixture
async def setup_env(tmp_path: Path):
    db_path = tmp_path / "obs_tx.db"
    con = sqlite3.connect(db_path)
    schema_path = Path(__file__).resolve().parent.parent.parent / "src/hunter/core/schema.sql"
    con.executescript(schema_path.read_text(encoding="utf-8"))
    con.close()

    mgr = StorageConnectionManager(StorageConfig(db_path=db_path))
    writer = SQLiteWriterActor(mgr)
    await writer.start()
    yield writer, mgr, db_path
    await writer.stop()
    await mgr.drain_and_close()


def _batch(
    query_id: str,
    lot_id: str,
    price: int | None,
    ts: str = "2026-10-23T10:00:00Z",
    state: PriceState = PriceState.ACTIVE,
) -> ObservationBatch:
    listing = ObservedListing(lot_id, "Item", price, state, f"https://avito.ru/{lot_id}")
    return ObservationBatch("slot_1", query_id, ts, ParseOutcome.OK, (listing,))


@pytest.mark.asyncio
async def test_repeat_batch_deduplication(setup_env) -> None:
    writer, mgr, _ = setup_env
    b = _batch("q_mac", "item_1", 50000)

    # 1st ingest -> generates NEW_LISTING
    res1 = await writer.submit(lambda c: execute_observation_tx(c, b, request_sequence=1))
    assert res1.observed_count == 1
    assert len(res1.events_generated) == 1
    assert res1.events_generated[0].event_type == EventType.NEW_LISTING

    # 2nd ingest of exact same batch -> 0 events
    res2 = await writer.submit(lambda c: execute_observation_tx(c, b, request_sequence=2))
    assert res2.observed_count == 1
    assert len(res2.events_generated) == 0


@pytest.mark.asyncio
async def test_stale_response_sequence_rejected(setup_env) -> None:
    writer, mgr, _ = setup_env
    b1 = _batch("q_mac", "item_1", 50000)
    await writer.submit(lambda c: execute_observation_tx(c, b1, request_sequence=10))

    # Older sequence 5 must be rejected
    b_stale = _batch("q_mac", "item_1", 45000)
    res_stale = await writer.submit(
        lambda c: execute_observation_tx(c, b_stale, request_sequence=5)
    )
    assert res_stale.stale_sequence is True
    assert len(res_stale.events_generated) == 0


@pytest.mark.asyncio
async def test_stale_overlap_does_not_drop_fresh_listings_in_same_batch(setup_env) -> None:
    writer, mgr, _ = setup_env
    await writer.submit(lambda c: execute_observation_tx(
        c, _batch("q_newer", "shared_lot", 50000), request_sequence=10
    ))
    batch = ObservationBatch(
        "slot_1", "q_older", "2026-10-23T10:01:00Z", ParseOutcome.OK,
        (
            ObservedListing("shared_lot", "Shared", 49000, PriceState.ACTIVE, "https://avito.ru/shared"),
            ObservedListing("fresh_lot", "Fresh", 30000, PriceState.ACTIVE, "https://avito.ru/fresh"),
        ),
    )

    result = await writer.submit(lambda c: execute_observation_tx(c, batch, request_sequence=9))

    assert result.stale_sequence is True
    assert result.observed_count == 1
    assert [event.avito_id for event in result.events_generated] == ["fresh_lot"]
    reader = await mgr.open_read_connection()
    try:
        rows = await (await reader.execute(
            "SELECT avito_id, known_price FROM listing_state ORDER BY avito_id"
        )).fetchall()
        assert [tuple(row) for row in rows] == [("fresh_lot", 30000), ("shared_lot", 50000)]
    finally:
        await mgr.close_reader(reader)


@pytest.mark.asyncio
async def test_concurrent_query_overlap_and_membership(setup_env) -> None:
    writer, mgr, _ = setup_env
    b_q1 = _batch("q_macbook", "item_shared", 70000)
    b_q2 = _batch("q_laptop", "item_shared", 70000)

    await writer.submit(lambda c: execute_observation_tx(c, b_q1, request_sequence=1))
    await writer.submit(lambda c: execute_observation_tx(c, b_q2, request_sequence=2))

    # Check query_membership contains both queries
    reader = await mgr.open_read_connection()
    cur = await reader.execute(
        "SELECT query_id FROM query_membership WHERE avito_id = 'item_shared'"
    )
    qids = {r[0] for r in await cur.fetchall()}
    assert qids == {"q_macbook", "q_laptop"}
    await mgr.close_reader(reader)


@pytest.mark.asyncio
async def test_missing_price_preserves_known_price(setup_env) -> None:
    writer, mgr, _ = setup_env
    b_known = _batch("q_mac", "item_price", 60000)
    await writer.submit(lambda c: execute_observation_tx(c, b_known, request_sequence=1))

    # Later observation with unknown price
    b_unknown = _batch("q_mac", "item_price", None, state=PriceState.UNKNOWN)
    await writer.submit(lambda c: execute_observation_tx(c, b_unknown, request_sequence=2))

    reader = await mgr.open_read_connection()
    cur = await reader.execute(
        "SELECT known_price FROM listing_state WHERE avito_id = 'item_price'"
    )
    row = await cur.fetchone()
    # 60000 preserved!
    assert row[0] == 60000
    await mgr.close_reader(reader)


@pytest.mark.asyncio
async def test_fault_rollback_leaves_no_state(setup_env) -> None:
    writer, mgr, _ = setup_env
    b = _batch("q_err", "item_err", 10000)

    async def _failing_tx(conn: aiosqlite.Connection):
        await execute_observation_tx(conn, b, request_sequence=1)
        raise RuntimeError("Crash before commit")

    with pytest.raises(RuntimeError, match="Crash before commit"):
        await writer.submit(_failing_tx)

    reader = await mgr.open_read_connection()
    cur = await reader.execute("SELECT count(*) FROM listing_state WHERE avito_id = 'item_err'")
    assert (await cur.fetchone())[0] == 0
    await mgr.close_reader(reader)


@pytest.mark.asyncio
async def test_fault_empty_baseline_rollback_and_replay(setup_env) -> None:
    writer, mgr, db_path = setup_env
    await writer.submit(lambda c: c.execute(
        "INSERT INTO queries (id, canonical_url, bootstrap_state) "
        "VALUES ('q_empty', 'https://avito.ru/q_empty', 'pending')"
    ))
    empty = ObservationBatch(
        "slot_1", "q_empty", "2026-10-23T10:00:00Z", ParseOutcome.EMPTY, ()
    )

    async def _crash_before_commit(conn: aiosqlite.Connection):
        await execute_observation_tx(conn, empty, request_sequence=1)
        raise RuntimeError("crash before empty baseline commit")

    with pytest.raises(RuntimeError, match="before empty baseline commit"):
        await writer.submit(_crash_before_commit)
    reader = await mgr.open_read_connection()
    try:
        row = await (await reader.execute(
            "SELECT bootstrap_state FROM queries WHERE id = 'q_empty'"
        )).fetchone()
        assert row[0] == "pending"
    finally:
        await mgr.close_reader(reader)

    baseline = await writer.submit(
        lambda c: execute_observation_tx(c, empty, request_sequence=2)
    )
    assert baseline.is_bootstrap is True
    reader = await mgr.open_read_connection()
    try:
        row = await (await reader.execute(
            "SELECT bootstrap_state FROM queries WHERE id = 'q_empty'"
        )).fetchone()
        assert row[0] == "ready"
    finally:
        await mgr.close_reader(reader)

    listing = ObservationBatch(
        "slot_1", "q_empty", "2026-10-23T10:01:00Z", ParseOutcome.OK,
        (ObservedListing("new_after_empty", "New", 9000, PriceState.ACTIVE, "https://avito.ru/new_after_empty"),),
    )
    restarted_mgr = StorageConnectionManager(StorageConfig(db_path=db_path))
    restarted_writer = SQLiteWriterActor(restarted_mgr)
    await restarted_writer.start()
    try:
        result = await restarted_writer.submit(
            lambda c: execute_observation_tx(c, listing, request_sequence=3)
        )
    finally:
        await restarted_writer.stop()
        await restarted_mgr.drain_and_close()
    assert [event.event_type for event in result.events_generated] == [EventType.NEW_LISTING]


@pytest.mark.asyncio
async def test_unknown_empty_does_not_complete_bootstrap(setup_env) -> None:
    writer, mgr, _ = setup_env
    await writer.submit(lambda c: c.execute(
        "INSERT INTO queries (id, canonical_url, bootstrap_state) "
        "VALUES ('q_unknown', 'https://avito.ru/q_unknown', 'pending')"
    ))
    unknown = ObservationBatch(
        "slot_1", "q_unknown", "2026-10-23T10:00:00Z", ParseOutcome.NETWORK_ERROR, ()
    )
    result = await writer.submit(
        lambda c: execute_observation_tx(c, unknown, request_sequence=1)
    )
    assert not result.is_bootstrap
    reader = await mgr.open_read_connection()
    try:
        row = await (await reader.execute(
            "SELECT bootstrap_state FROM queries WHERE id = 'q_unknown'"
        )).fetchone()
        assert row[0] == "pending"
    finally:
        await mgr.close_reader(reader)


@pytest.mark.asyncio
async def test_public_success_timestamp_requires_current_assignment(setup_env) -> None:
    writer, mgr, _ = setup_env
    await writer.submit(
        lambda c: c.executescript(
            "INSERT INTO queries (id, canonical_url, bootstrap_state) "
            "VALUES ('q_public', 'https://avito.ru/q_public', 'pending');"
            "INSERT INTO slots (id, endpoint, cookie_namespace) "
            "VALUES ('slot_public', 'http://proxy.invalid:1', 'test');"
            "INSERT INTO assignments (id, slot_id, query_id, position, active, generation) "
            "VALUES ('assignment_public', 'slot_public', 'q_public', 0, 1, 3);"
        )
    )
    batch = ObservationBatch(
        "slot_public", "q_public", "2026-10-23T10:00:00Z", ParseOutcome.EMPTY, (),
        assignment_generation=3,
    )
    await writer.submit(lambda c: execute_observation_tx(c, batch, enforce_assignment=True))
    reader = await mgr.open_read_connection()
    try:
        row = await (await reader.execute(
            "SELECT value FROM runtime WHERE key = 'query:last_successful_poll:q_public'"
        )).fetchone()
        assert row[0] == batch.observed_at
    finally:
        await mgr.close_reader(reader)

    stale = ObservationBatch(
        "slot_public", "q_public", "2026-10-23T10:01:00Z", ParseOutcome.EMPTY, (),
        assignment_generation=2,
    )
    result = await writer.submit(
        lambda c: execute_observation_tx(c, stale, enforce_assignment=True)
    )
    assert result.stale_assignment
    reader = await mgr.open_read_connection()
    try:
        row = await (await reader.execute(
            "SELECT value FROM runtime WHERE key = 'query:last_successful_poll:q_public'"
        )).fetchone()
        assert row[0] == batch.observed_at
    finally:
        await mgr.close_reader(reader)
