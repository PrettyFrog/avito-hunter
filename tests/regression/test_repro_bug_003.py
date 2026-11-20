"""Regression test for BUG-003: Slot backoff for 429/challenge instead of 1-hour quarantine (<= 400 lines)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import sqlite3
import pytest

from hunter.application.capacity import CapacityAdmissionController
from hunter.application.slot_supervisor import SlotSupervisor
from hunter.domain.slots import SlotHealth, SlotRole, SlotSignal
from hunter.storage.connection import StorageConfig, StorageConnectionManager
from hunter.storage.slot_state import SlotRepository
from hunter.storage.writer import SQLiteWriterActor


@pytest.mark.asyncio
async def test_slot_429_uses_short_backoff_and_recovers(tmp_path) -> None:
    db_path = tmp_path / "test_bug_003.db"
    con = sqlite3.connect(db_path)
    schema_sql = open("src/hunter/core/schema.sql", encoding="utf-8").read()
    con.executescript(schema_sql)
    con.close()

    mgr = StorageConnectionManager(StorageConfig(db_path=db_path))
    writer = SQLiteWriterActor(mgr)
    await writer.start()
    slot_repo = SlotRepository(mgr, writer)
    controller = CapacityAdmissionController(max_queries_per_slot=4)
    supervisor = SlotSupervisor(slot_repo, controller)

    try:
        await slot_repo.register_slot("slot-1", "direct", role=SlotRole.ACTIVE)
        # Signal 429
        now = datetime.now(UTC)
        await supervisor.handle_slot_signal("slot-1", SlotSignal.HTTP_429)

        slot = await slot_repo.get_slot("slot-1")
        assert slot is not None
        assert slot.quarantined_until is not None

        # Deadline must NOT be 1 hour (3600s). It must be a short backoff (e.g. <= 60s).
        deadline_dt = datetime.fromisoformat(slot.quarantined_until)
        if deadline_dt.tzinfo is None:
            deadline_dt = deadline_dt.replace(tzinfo=UTC)
        backoff_sec = (deadline_dt - now).total_seconds()
        assert backoff_sec <= 65.0, f"Expected short backoff <= 65s, got {backoff_sec}s"

        # Check recovery after deadline
        future_ts = (now + timedelta(seconds=70)).isoformat()
        recovered = await supervisor.check_quarantined_slots(now_ts=future_ts)
        assert "slot-1" in recovered

        recovered_slot = await slot_repo.get_slot("slot-1")
        assert recovered_slot.health == SlotHealth.READY
        assert recovered_slot.quarantined_until is None
    finally:
        await writer.stop()
        await mgr.drain_and_close()


@pytest.mark.asyncio
async def test_slot_challenge_recovers_after_deadline(tmp_path) -> None:
    db_path = tmp_path / "test_bug_003_challenge.db"
    con = sqlite3.connect(db_path)
    schema_sql = open("src/hunter/core/schema.sql", encoding="utf-8").read()
    con.executescript(schema_sql)
    con.close()

    mgr = StorageConnectionManager(StorageConfig(db_path=db_path))
    writer = SQLiteWriterActor(mgr)
    await writer.start()
    slot_repo = SlotRepository(mgr, writer)
    controller = CapacityAdmissionController(max_queries_per_slot=4)
    supervisor = SlotSupervisor(slot_repo, controller)

    try:
        await slot_repo.register_slot("slot-chal", "direct", role=SlotRole.ACTIVE)
        now = datetime.now(UTC)
        await supervisor.handle_slot_signal("slot-chal", SlotSignal.CHALLENGE_DETECTED)

        slot = await slot_repo.get_slot("slot-chal")
        assert slot.health == SlotHealth.CHALLENGE

        # check_quarantined_slots must recover challenge slot after deadline
        future_ts = (now + timedelta(seconds=100)).isoformat()
        recovered = await supervisor.check_quarantined_slots(now_ts=future_ts)
        assert "slot-chal" in recovered

        recovered_slot = await slot_repo.get_slot("slot-chal")
        assert recovered_slot.health == SlotHealth.READY
    finally:
        await writer.stop()
        await mgr.drain_and_close()
