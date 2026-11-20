"""An observation from a superseded slot assignment cannot mutate listing state."""

from dataclasses import replace
import sqlite3
from pathlib import Path

import aiosqlite
import pytest

from hunter.domain.observations import (
    ObservationBatch,
    ObservedListing,
    ParseOutcome,
    PriceState,
)
from hunter.domain.slots import SlotRole
from hunter.storage.connection import StorageConfig, StorageConnectionManager
from hunter.storage.observation_tx import execute_observation_tx
from hunter.storage.slot_state import SlotRepository
from hunter.storage.writer import SQLiteWriterActor


@pytest.fixture
async def storage(tmp_path: Path):
    db_path = tmp_path / "generation.db"
    schema = Path("src/hunter/core/schema.sql").read_text(encoding="utf-8")
    with sqlite3.connect(db_path) as conn:
        conn.executescript(schema)
    manager = StorageConnectionManager(StorageConfig(db_path=db_path))
    writer = SQLiteWriterActor(manager)
    await writer.start()
    repo = SlotRepository(manager, writer)
    try:
        yield manager, writer, repo
    finally:
        await writer.stop()
        await manager.drain_and_close()


def _batch(listing_id: str, generation: int, observed_at: str) -> ObservationBatch:
    listing = ObservedListing(
        listing_id,
        "Generation test item",
        500,
        PriceState.ACTIVE,
        f"https://avito.ru/{listing_id}",
    )
    return ObservationBatch(
        "source",
        "q_generation",
        observed_at,
        ParseOutcome.OK,
        (listing,),
        assignment_generation=generation,
    )


def test_assignment_generation_roundtrips_in_observation_payload() -> None:
    batch = _batch("lot", 7, "2026-10-25T10:00:00+00:00")
    batch = replace(batch, tenant_generations={9001: 3})
    assert ObservationBatch.from_dict(batch.to_dict()) == batch


@pytest.mark.asyncio
async def test_superseded_generation_is_rejected_before_listing_mutation(storage) -> None:
    _, writer, slots = storage
    await slots.register_slot("source", "http://source:8080", role=SlotRole.ACTIVE)
    await slots.register_slot("target", "http://target:8080", role=SlotRole.RESERVE)
    source_generation = await slots.assign_query("source", "q_generation")

    async def seed(c: aiosqlite.Connection) -> None:
        await c.execute(
            "INSERT INTO subscriptions (id, user_id, query_id, max_price) "
            "VALUES ('sub', 9001, 'q_generation', 1000)"
        )
        await c.execute(
            "INSERT INTO runtime (key, value) "
            'VALUES (\'cutover:tenant:9001\', \'{"mode":"v2","cutoff_sequence":0}\')'
        )

    await writer.submit(seed)

    baseline = _batch("base_lot", source_generation, "2026-10-25T10:00:00+00:00")
    await writer.submit(
        lambda c: execute_observation_tx(c, baseline, request_sequence=1, enforce_assignment=True)
    )
    target_generation = await slots.transfer_to_spare("source", "target", "q_generation")

    stale = await writer.submit(
        lambda c: execute_observation_tx(
            c,
            _batch("late_lot", source_generation, "2026-10-25T10:00:30+00:00"),
            request_sequence=2,
            enforce_assignment=True,
        )
    )
    current = await writer.submit(
        lambda c: execute_observation_tx(
            c,
            ObservationBatch(
                "target",
                "q_generation",
                "2026-10-25T10:00:31+00:00",
                ParseOutcome.OK,
                (_batch("late_lot", target_generation, "2026-10-25T10:00:31+00:00").listings[0],),
                assignment_generation=target_generation,
            ),
            request_sequence=3,
            enforce_assignment=True,
        )
    )

    assert stale.stale_assignment is True
    assert stale.events_generated == () and stale.deliveries_created == 0
    assert current.stale_assignment is False and current.deliveries_created == 1
    reader = await storage[0].open_read_connection()
    try:
        row = await (
            await reader.execute(
                "SELECT known_price FROM listing_state WHERE avito_id = 'late_lot'"
            )
        ).fetchone()
        assert row[0] == 500
    finally:
        await storage[0].close_reader(reader)
