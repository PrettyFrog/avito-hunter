"""A failed spare transfer cannot strand or duplicate a query assignment."""

import asyncio
from pathlib import Path
import sqlite3

import pytest

from hunter.core.db import Database
from hunter.domain.slots import CapacityExceededError, SlotCapacityError
from hunter.storage.connection import StorageConfig, StorageConnectionManager
from hunter.storage.slot_state import SlotRepository
from hunter.storage.writer import SQLiteWriterActor


@pytest.fixture
async def slots(tmp_path: Path):
    path = tmp_path / "slots.db"
    db = Database(path)
    await db.connect()
    mgr = StorageConnectionManager(StorageConfig(db_path=path))
    writer = SQLiteWriterActor(mgr)
    await writer.start()
    repo = SlotRepository(mgr, writer)
    try:
        yield repo
    finally:
        await writer.stop()
        await mgr.drain_and_close()
        await db.close()


@pytest.mark.asyncio
async def test_full_target_preserves_source_assignment(slots: SlotRepository) -> None:
    await slots.register_slot("source", "http://source:8080")
    await slots.register_slot("target", "http://target:8080")
    await slots.assign_query("source", "transfer_me")
    for i in range(4):
        await slots.assign_query("target", f"occupied_{i}")
    before = await slots.get_slot("source")

    with pytest.raises(CapacityExceededError):
        await slots.transfer_to_spare("source", "target", "transfer_me")

    after = await slots.get_slot("source")
    assert after.capacity.assigned_queries == ("transfer_me",)
    assert after.generation == before.generation
    assert len((await slots.get_slot("target")).capacity.assigned_queries) == 4


@pytest.mark.asyncio
async def test_concurrent_transfers_have_one_owner(slots: SlotRepository) -> None:
    await slots.register_slot("source", "http://source:8080")
    await slots.register_slot("target_a", "http://target-a:8080")
    await slots.register_slot("target_b", "http://target-b:8080")
    await slots.assign_query("source", "transfer_me")
    source_before = await slots.get_slot("source")

    results = await asyncio.gather(
        slots.transfer_to_spare("source", "target_a", "transfer_me"),
        slots.transfer_to_spare("source", "target_b", "transfer_me"),
        return_exceptions=True,
    )
    assert sum(isinstance(result, int) for result in results) == 1
    assert sum(isinstance(result, SlotCapacityError) for result in results) == 1
    owners = [slot for slot in ("source", "target_a", "target_b")
              if "transfer_me" in (await slots.get_slot(slot)).capacity.assigned_queries]
    assert len(owners) == 1 and owners[0] != "source"
    assert (await slots.get_slot("source")).generation == source_before.generation + 1


@pytest.mark.asyncio
async def test_insert_failure_rolls_back_both_generations(slots: SlotRepository) -> None:
    await slots.register_slot("source", "http://source:8080")
    await slots.register_slot("target", "http://target:8080")
    await slots.assign_query("source", "transfer_me")
    before_source = await slots.get_slot("source")
    before_target = await slots.get_slot("target")

    async def reject_target(conn) -> None:
        await conn.execute("""
            CREATE TRIGGER reject_target_transfer BEFORE INSERT ON assignments
            WHEN NEW.slot_id = 'target'
            BEGIN SELECT RAISE(ABORT, 'injected target failure'); END
        """)

    await slots.writer.submit(reject_target)
    with pytest.raises(sqlite3.IntegrityError, match="injected target failure"):
        await slots.transfer_to_spare("source", "target", "transfer_me")
    after_source = await slots.get_slot("source")
    after_target = await slots.get_slot("target")
    assert after_source.capacity == before_source.capacity
    assert after_source.generation == before_source.generation
    assert after_target.generation == before_target.generation
