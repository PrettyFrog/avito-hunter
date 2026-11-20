"""Regression test for F-001: Expired slot quarantine is never recovered, causing perpetual DoS."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
import aiosqlite
import pytest

from hunter.application.slot_supervisor import SlotSupervisor
from hunter.domain.slots import SlotHealth
from hunter.storage.connection import StorageConfig, StorageConnectionManager
from hunter.storage.slot_state import SlotRepository
from hunter.storage.writer import SQLiteWriterActor


@pytest.mark.asyncio
async def test_repro_F_001_quarantine_expiration_recovers_slot(clean_db_path: Path) -> None:
    schema_path = Path(__file__).resolve().parents[2] / "src/hunter/core/schema.sql"
    with open(schema_path, "r", encoding="utf-8") as sf:
        schema_sql = sf.read()

    past_time = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    async with aiosqlite.connect(clean_db_path) as c:
        await c.executescript(schema_sql)
        await c.execute(
            "INSERT INTO slots (id, endpoint, cookie_namespace, health_state, quarantine_until, active_generation) "
            "VALUES ('slot_q', 'http://proxy1:8080', 'ns_q', 'quarantined', ?, 1)",
            (past_time,),
        )
        await c.commit()

    mgr = StorageConnectionManager(StorageConfig(db_path=clean_db_path))
    writer = SQLiteWriterActor(mgr)
    await writer.start()
    try:
        repo = SlotRepository(mgr, writer)
        supervisor = SlotSupervisor(repo)

        # Expected behavior: supervisor or repo should check and recover expired quarantined slots
        assert hasattr(supervisor, "check_quarantined_slots"), "SlotSupervisor missing check_quarantined_slots (F-001)"
        await supervisor.check_quarantined_slots()

        slot = await repo.get_slot("slot_q")
        assert slot is not None
        assert slot.health in (SlotHealth.READY, SlotHealth.WARMING), f"Slot remained quarantined: {slot.health} (F-001)"
    finally:
        await writer.stop()
