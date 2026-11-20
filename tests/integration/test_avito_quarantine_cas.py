"""Integration tests for CAS quarantine recovery and stale snapshot fencing in SlotSupervisor (T-976)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
import sqlite3
from unittest.mock import AsyncMock

import pytest

from hunter.application.slot_supervisor import SlotSupervisor
from hunter.domain.slots import SlotHealth, SlotSignal
from hunter.storage.connection import StorageConfig, StorageConnectionManager
from hunter.storage.slot_state import SlotRepository
from hunter.storage.writer import SQLiteWriterActor


@pytest.fixture
async def slot_env(tmp_path: Path):
    db = tmp_path / "t.db"
    with sqlite3.connect(db) as c:
        c.executescript(Path("src/hunter/core/schema.sql").read_text())
    mgr = StorageConnectionManager(StorageConfig(db_path=db))
    writer = SQLiteWriterActor(mgr)
    await writer.start()
    try:
        yield SlotRepository(mgr, writer), mgr, writer
    finally:
        await writer.stop()
        await mgr.drain_and_close()


@pytest.mark.asyncio
async def test_extended_quarantine_not_cleared_by_stale_snapshot(slot_env) -> None:
    """When a slot's quarantine is extended after list_slots, stale CAS must reject recovery to READY."""
    repo, _, _ = slot_env
    await repo.register_slot("slot_cas", "direct")
    sup = SlotSupervisor(repo)

    # 1. Quarantine slot in the past (expired)
    past_ts = (datetime.now(UTC) - timedelta(seconds=10)).isoformat()
    await repo.set_health("slot_cas", SlotHealth.QUARANTINED, quarantined_until=past_ts)

    # Mock list_slots to return the expired snapshot, but extend live quarantine before recovery
    orig_list_slots = repo.list_slots
    callback_mock = AsyncMock()
    sup.on_assignment_changed = callback_mock

    async def _racing_list_slots():
        snapshots = await orig_list_slots()
        # Race: extend live quarantine by 1 hour
        future_ts = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        await repo.set_health("slot_cas", SlotHealth.QUARANTINED, quarantined_until=future_ts)
        return snapshots

    repo.list_slots = _racing_list_slots

    recovered = await sup.check_quarantined_slots()

    # Recovery must fail because snapshot is stale!
    assert "slot_cas" not in recovered
    callback_mock.assert_not_called()

    live_slot = await repo.get_slot("slot_cas")
    assert live_slot is not None
    assert live_slot.health == SlotHealth.QUARANTINED
    assert datetime.fromisoformat(live_slot.quarantined_until) > datetime.now(UTC)


@pytest.mark.asyncio
async def test_valid_expired_quarantine_recovers_to_ready(slot_env) -> None:
    """When quarantine has genuinely expired without racing updates, CAS transitions slot to READY."""
    repo, _, _ = slot_env
    await repo.register_slot("slot_ok", "direct")
    sup = SlotSupervisor(repo)

    past_ts = (datetime.now(UTC) - timedelta(seconds=10)).isoformat()
    await repo.set_health("slot_ok", SlotHealth.QUARANTINED, quarantined_until=past_ts)

    recovered = await sup.check_quarantined_slots()
    assert "slot_ok" in recovered

    live_slot = await repo.get_slot("slot_ok")
    assert live_slot is not None
    assert live_slot.health == SlotHealth.READY
    assert live_slot.quarantined_until is None


@pytest.mark.asyncio
async def test_disabled_slot_not_cleared_by_stale_quarantine(slot_env) -> None:
    """When a slot is disabled while quarantined, stale quarantine check must not recover it to READY."""
    repo, _, _ = slot_env
    await repo.register_slot("slot_dis", "direct")
    sup = SlotSupervisor(repo)

    past_ts = (datetime.now(UTC) - timedelta(seconds=10)).isoformat()
    await repo.set_health("slot_dis", SlotHealth.QUARANTINED, quarantined_until=past_ts)

    orig_list_slots = repo.list_slots
    async def _racing_list_slots():
        snaps = await orig_list_slots()
        await repo.set_health("slot_dis", SlotHealth.DISABLED)
        return snaps

    repo.list_slots = _racing_list_slots
    recovered = await sup.check_quarantined_slots()
    assert "slot_dis" not in recovered

    live_slot = await repo.get_slot("slot_dis")
    assert live_slot is not None
    assert live_slot.health == SlotHealth.DISABLED

