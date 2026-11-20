"""Unit tests for SlotSupervisor failover, quarantine, and cookie isolation (S5 / T-482).

Covers:
- Model with 25 active slots + 2 reserve slots.
- Protocol: persists state -> cancels old -> increments generation -> assigns spare.
- Two failures absorbed by two reserve slots.
- Third failure results in DEGRADED status without overloading neighbors (> 4).
- Cookies are strictly isolated per IP/slot and never copied across endpoints.
- Active slot worker cancellation on quarantine transition.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from hunter.application.capacity import CapacityAdmissionController
from hunter.application.slot_supervisor import SlotSupervisor
from hunter.domain.slots import SlotHealth, SlotRole, SlotSignal
from hunter.storage.connection import StorageConfig, StorageConnectionManager
from hunter.storage.slot_state import SlotRepository
from hunter.storage.writer import SQLiteWriterActor
from hunter.transport.session_store import EndpointMismatchError, SessionStore

SCHEMA_PATH = Path("src/hunter/core/schema.sql")


@pytest.fixture
async def failover_env(tmp_path: Path):
    db_path = tmp_path / "test_failover.db"
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    con.close()

    mgr = StorageConnectionManager(StorageConfig(db_path=db_path))
    writer = SQLiteWriterActor(mgr)
    await writer.start()
    repo = SlotRepository(mgr, writer)
    session_store = SessionStore(base_dir=tmp_path / "sessions")
    controller = CapacityAdmissionController(max_queries_per_slot=4)
    workers: dict[str, AsyncMock] = {}
    supervisor = SlotSupervisor(repo, controller, workers)

    yield repo, supervisor, session_store, workers, mgr, writer

    await writer.stop()
    await mgr.drain_and_close()


@pytest.mark.asyncio
async def test_25_slots_two_failures_absorbed_third_degraded(failover_env):
    repo, supervisor, _, _, _, _ = failover_env

    # 1. Register 25 active slots and fill them with 4 queries each (100 total)
    for i in range(25):
        sid = f"act_{i:02d}"
        await repo.register_slot(sid, f"http://10.0.0.{i+1}:8080", role=SlotRole.ACTIVE)
        for q in range(4):
            await repo.assign_query(sid, f"q_{i}_{q}")

    # 2. Register 2 reserve slots (empty, ready for failover)
    await repo.register_slot("res_01", "http://10.0.0.101:8080", role=SlotRole.RESERVE)
    await repo.register_slot("res_02", "http://10.0.0.102:8080", role=SlotRole.RESERVE)

    # Failure 1: Slot act_00 fails with HTTP_403
    out1 = await supervisor.handle_slot_signal("act_00", SlotSignal.HTTP_403)
    assert out1.success is True
    assert out1.new_health == SlotHealth.QUARANTINED
    assert out1.status == "failover_complete"
    assert out1.target_slot_id == "res_01"
    assert len(out1.transferred_queries) == 4

    # Failure 2: Slot act_01 fails with CHALLENGE_DETECTED
    out2 = await supervisor.handle_slot_signal("act_01", SlotSignal.CHALLENGE_DETECTED)
    assert out2.success is True
    assert out2.new_health == SlotHealth.CHALLENGE
    assert out2.status == "failover_complete"
    assert out2.target_slot_id == "res_02"
    assert len(out2.transferred_queries) == 4

    # Failure 3: Slot act_02 fails with HTTP_429. Both reserve slots now at capacity!
    out3 = await supervisor.handle_slot_signal("act_02", SlotSignal.HTTP_429)
    assert out3.success is False
    assert out3.status == "degraded"
    assert len(out3.untransferred_queries) == 4
    assert "exhausted" in out3.reason

    # Verify no healthy slot was overloaded beyond capacity 4
    for i in range(3, 25):
        s = await repo.get_slot(f"act_{i:02d}")
        assert s is not None and s.capacity.current_load == 4


@pytest.mark.asyncio
async def test_cookies_never_transferred_across_slot_endpoints(failover_env):
    repo, supervisor, session_store, _, _, _ = failover_env

    # Setup active slot with dedicated cookies bound to IP 10.0.0.1
    await repo.register_slot("act_sec", "http://10.0.0.1:8080", role=SlotRole.ACTIVE)
    await repo.assign_query("act_sec", "q_secret")
    await session_store.save_session(
        slot_id="act_sec",
        endpoint="http://10.0.0.1:8080",
        version=1,
        generation=1,
        cookies=[{"name": "auth_cookie", "value": "sec_token_ip1"}],
    )

    # Reserve slot bound to IP 10.0.0.200
    await repo.register_slot("res_sec", "http://10.0.0.200:8080", role=SlotRole.RESERVE)

    # Failover query to reserve
    out = await supervisor.handle_slot_signal("act_sec", SlotSignal.HTTP_403)
    assert out.success is True
    assert out.target_slot_id == "res_sec"

    # Reserve slot does NOT receive or inherit act_sec's cookies
    assert session_store.has_session("res_sec") is False
    # act_sec cookies remain safely isolated on its original endpoint
    with pytest.raises(EndpointMismatchError):
        await session_store.load_session("act_sec", expected_endpoint="http://10.0.0.200:8080")


@pytest.mark.asyncio
async def test_worker_cancelled_on_quarantine(failover_env):
    repo, supervisor, _, workers, _, _ = failover_env

    await repo.register_slot("act_w", "http://10.0.0.5:8080", role=SlotRole.ACTIVE)
    await repo.register_slot("res_w", "http://10.0.0.105:8080", role=SlotRole.RESERVE)

    mock_worker = AsyncMock()
    workers["act_w"] = mock_worker

    out = await supervisor.handle_slot_signal("act_w", SlotSignal.HTTP_403)
    assert out.success is True
    # Worker cancellation was called
    assert mock_worker.stop.call_count == 1


@pytest.mark.asyncio
async def test_recovered_slot_notifies_assignment_changed(failover_env):
    repo, supervisor, _, _, _, _ = failover_env

    await repo.register_slot("act_rec", "http://10.0.0.6:8080", role=SlotRole.ACTIVE)
    await repo.assign_query("act_rec", "q_rec1")
    await repo.set_health("act_rec", SlotHealth.CHALLENGE, quarantined_until="2026-01-01T00:00:00Z")

    notified = []
    supervisor.on_assignment_changed = AsyncMock(side_effect=lambda qid: notified.append(qid))

    recovered = await supervisor.check_quarantined_slots("2026-01-02T00:00:00Z")
    assert recovered == ["act_rec"]
    assert notified == ["q_rec1"]


@pytest.mark.asyncio
async def test_active_slots_rebalance_after_quarantine_recovery(failover_env):
    repo, supervisor, _, _, _, _ = failover_env

    # 1. act_a has 2 queries, act_b is quarantined with 0 queries
    await repo.register_slot("act_a", "http://10.0.0.1:8080", role=SlotRole.ACTIVE)
    await repo.register_slot("act_b", "http://10.0.0.2:8080", role=SlotRole.ACTIVE)
    await repo.assign_query("act_a", "q_1")
    await repo.assign_query("act_a", "q_2")
    await repo.set_health("act_b", SlotHealth.QUARANTINED, quarantined_until="2026-01-01T00:00:00Z")

    rebalanced_qids = []
    supervisor.on_assignment_changed = AsyncMock(side_effect=lambda qid: rebalanced_qids.append(qid))

    # 2. Quarantine expires: act_b recovers and auto-rebalances load from act_a
    recovered = await supervisor.check_quarantined_slots("2026-01-02T00:00:00Z")
    assert recovered == ["act_b"]

    slot_a = await repo.get_slot("act_a")
    slot_b = await repo.get_slot("act_b")
    assert slot_a.capacity.current_load == 1
    assert slot_b.capacity.current_load == 1
    assert rebalanced_qids == ["q_2"]


@pytest.mark.asyncio
async def test_active_slots_rebalance_on_startup_tick(failover_env):
    repo, supervisor, _, _, _, _ = failover_env

    # Both slots READY, but all 2 queries on act_a and 0 on act_b
    await repo.register_slot("act_a", "http://10.0.0.1:8080", role=SlotRole.ACTIVE)
    await repo.register_slot("act_b", "http://10.0.0.2:8080", role=SlotRole.ACTIVE)
    await repo.assign_query("act_a", "q_1")
    await repo.assign_query("act_a", "q_2")

    recovered = await supervisor.check_quarantined_slots()
    assert recovered == []

    slot_a = await repo.get_slot("act_a")
    slot_b = await repo.get_slot("act_b")
    assert slot_a.capacity.current_load == 1
    assert slot_b.capacity.current_load == 1


