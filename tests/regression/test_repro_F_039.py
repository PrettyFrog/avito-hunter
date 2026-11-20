"""Regression test for F-039: Cross-query notification suppression.

When Query 1 observes listing A, subsequent observations of listing A by Query 2
must not suppress the NEW_LISTING event for Query 2 subscribers.
"""

from __future__ import annotations

from pathlib import Path
import aiosqlite
import pytest

from hunter.core.models import ObservedListing, PriceState
from hunter.domain.observations import ObservationBatch, ParseOutcome
from hunter.storage.connection import StorageConfig, StorageConnectionManager
from hunter.storage.observation_tx import execute_observation_tx
from hunter.storage.writer import SQLiteWriterActor


@pytest.mark.asyncio
async def test_repro_F_039_cross_query_suppression_prevented(initialized_db: Path) -> None:
    async with aiosqlite.connect(initialized_db) as init_conn:
        await init_conn.execute(
            "INSERT INTO slots (id, endpoint, cookie_namespace) VALUES ('s1', 'http://proxy:8080', 'ns1')"
        )
        await init_conn.execute(
            "INSERT INTO queries (id, canonical_url, bootstrap_state) VALUES ('q1', 'http://avito.ru/q1', 'ready')"
        )
        await init_conn.execute(
            "INSERT INTO queries (id, canonical_url, bootstrap_state) VALUES ('q2', 'http://avito.ru/q2', 'ready')"
        )
        await init_conn.execute(
            "INSERT INTO assignments (id, slot_id, query_id, position, generation, active) "
            "VALUES ('a1', 's1', 'q1', 0, 1, 1), ('a2', 's1', 'q2', 1, 2, 1)"
        )
        await init_conn.commit()

    cfg = StorageConfig(db_path=initialized_db)
    mgr = StorageConnectionManager(cfg)
    writer = SQLiteWriterActor(mgr)
    await writer.start()

    listing_a = ObservedListing(
        avito_id="123456789",
        title="Lenovo ThinkPad T14",
        price=50000,
        price_state=PriceState.ACTIVE,
        url="https://avito.ru/item/123456789",
    )

    try:
        # 1. Query 1 observes listing A
        batch_q1 = ObservationBatch(
            query_id="q1",
            slot_id="s1",
            assignment_generation=1,
            observed_at="2026-10-30T04:00:00Z",
            outcome=ParseOutcome.OK,
            listings=[listing_a],
        )
        res_q1 = await writer.submit(lambda c: execute_observation_tx(c, batch_q1, request_sequence=1))
        assert len(res_q1.events_generated) == 1, "Query 1 should produce 1 NEW_LISTING event"

        # 2. Query 2 observes listing A for the very first time!
        batch_q2 = ObservationBatch(
            query_id="q2",
            slot_id="s1",
            assignment_generation=2,
            observed_at="2026-10-30T04:05:00Z",
            outcome=ParseOutcome.OK,
            listings=[listing_a],
        )
        res_q2 = await writer.submit(lambda c: execute_observation_tx(c, batch_q2, request_sequence=2))

        # Expected behavior: Query 2 must produce a NEW_LISTING event for its subscribers
        assert len(res_q2.events_generated) == 1, (
            f"Query 2 produced {len(res_q2.events_generated)} events instead of 1 (F-039 cross-query suppression)"
        )
        assert res_q2.events_generated[0].avito_id == "123456789"
        assert res_q2.events_generated[0].query_id == "q2"
    finally:
        await writer.stop()
        await mgr.drain_and_close()
