"""Unit tests for SlotRepository (S5 / T-477).

Covers:
- Slot registration, retrieval, and session versioning.
- Capacity limit (<= 4) and reserve admission denial.
- Atomic generation bump before reassignment.
- Failover transfer to ready spare slot.
- Monotonic CAS barrier rejecting late request sequence responses.
- Manual disable persisting across automated probe signals.
- No cookie deletion during transitions.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from hunter.domain.slots import (
    CapacityExceededError,
    ReserveAdmissionError,
    SlotCapacityError,
    SlotHealth,
    SlotRole,
)
from hunter.storage.connection import StorageConfig, StorageConnectionManager
from hunter.storage.slot_state import SlotRepository
from hunter.storage.writer import SQLiteWriterActor

SCHEMA_PATH = Path("src/hunter/core/schema.sql")


@pytest.fixture
async def slot_env(tmp_path: Path):
    db_path = tmp_path / "test_slots.db"
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    con.close()

    mgr = StorageConnectionManager(StorageConfig(db_path=db_path))
    writer = SQLiteWriterActor(mgr)
    await writer.start()
    repo = SlotRepository(mgr, writer)
    yield repo, mgr, writer, db_path
    await writer.stop()
    await mgr.drain_and_close()


@pytest.mark.asyncio
async def test_slot_registration_and_retrieval(slot_env):
    repo, _, _, _ = slot_env
    slot = await repo.register_slot("slot_1", "http://10.0.0.1:8080")
    assert slot.slot_id == "slot_1"
    assert slot.endpoint == "http://10.0.0.1:8080"
    assert slot.health == SlotHealth.READY
    assert slot.role == SlotRole.ACTIVE
    assert slot.generation == 1
    assert slot.session_version == 1
    assert slot.capacity.current_load == 0


@pytest.mark.asyncio
async def test_capacity_limit_and_reserve_admission(slot_env):
    repo, _, _, _ = slot_env
    await repo.register_slot("slot_1", "http://10.0.0.1:8080")
    for i in range(4):
        gen = await repo.assign_query("slot_1", f"q_{i}")
        assert gen > 1

    slot = await repo.get_slot("slot_1")
    assert slot is not None
    assert slot.capacity.is_full is True
    assert slot.capacity.current_load == 4

    with pytest.raises(CapacityExceededError):
        await repo.assign_query("slot_1", "q_overflow")

    await repo.register_slot("slot_res_1", "http://10.0.0.2:8080", role=SlotRole.RESERVE)
    with pytest.raises(ReserveAdmissionError):
        await repo.assign_query("slot_res_1", "q_user")


@pytest.mark.asyncio
async def test_atomic_generation_bump_before_reassignment(slot_env):
    repo, _, _, _ = slot_env
    await repo.register_slot("slot_1", "http://10.0.0.1:8080")

    g1 = await repo.assign_query("slot_1", "q_alpha")
    s1 = await repo.get_slot("slot_1")
    assert s1 is not None and s1.generation == g1 == 2

    g2 = await repo.assign_query("slot_1", "q_beta")
    s2 = await repo.get_slot("slot_1")
    assert s2 is not None and s2.generation == g2 == 3

    # Re-assign existing query q_alpha
    g3 = await repo.assign_query("slot_1", "q_alpha")
    s3 = await repo.get_slot("slot_1")
    assert s3 is not None and s3.generation == g3 == 4


@pytest.mark.asyncio
async def test_transfer_to_ready_spare(slot_env):
    repo, _, _, _ = slot_env
    await repo.register_slot("slot_active_1", "http://10.0.0.1:8080")
    await repo.register_slot("slot_res_1", "http://10.0.0.2:8080")

    await repo.assign_query("slot_active_1", "q_migrating")
    s_before = await repo.get_slot("slot_active_1")
    assert s_before is not None and "q_migrating" in s_before.capacity.assigned_queries

    spare_gen = await repo.transfer_to_spare("slot_active_1", "slot_res_1", "q_migrating")
    assert spare_gen >= 2

    s_old = await repo.get_slot("slot_active_1")
    s_spare = await repo.get_slot("slot_res_1")
    assert s_old is not None and "q_migrating" not in s_old.capacity.assigned_queries
    assert s_spare is not None and "q_migrating" in s_spare.capacity.assigned_queries

    # Transfer to unhealthy spare fails
    await repo.register_slot("slot_res_bad", "http://10.0.0.3:8080")
    await repo.set_health("slot_res_bad", SlotHealth.QUARANTINED)
    with pytest.raises(SlotCapacityError):
        await repo.transfer_to_spare("slot_res_1", "slot_res_bad", "q_migrating")


@pytest.mark.asyncio
async def test_fault_retire_during_poll(slot_env):
    repo, _, _, db_path = slot_env
    await repo.register_slot("source", "http://10.0.0.1:8080")
    await repo.register_slot("reserve", "http://10.0.0.2:8080", role=SlotRole.RESERVE)
    await repo.bump_session_version("source", cookie_hash="retirement-cookie")
    generation = await repo.assign_query("source", "q_in_flight")
    started = asyncio.Event()
    release = asyncio.Event()

    async def delayed_signal() -> bool:
        started.set()
        await release.wait()
        return await repo.set_health_if_assignment_current(
            "source", "q_in_flight", generation, SlotHealth.QUARANTINED
        )

    poll = asyncio.create_task(delayed_signal())
    try:
        await started.wait()
        transferred, degraded = await repo.retire_slot("source")
        release.set()
        assert await poll is False
        assert transferred == ["q_in_flight"]
        assert degraded == []
        source = await repo.get_slot("source")
        reserve = await repo.get_slot("reserve")
        assert source.health == SlotHealth.DISABLED
        assert source.capacity.assigned_queries == ()
        assert reserve.capacity.assigned_queries == ("q_in_flight",)
        conn = sqlite3.connect(db_path)
        cookie = conn.execute(
            "SELECT cookie_hash FROM session_versions WHERE slot_id = 'source'"
        ).fetchone()
        conn.close()
        assert cookie == ("retirement-cookie",)
    finally:
        release.set()
        await asyncio.gather(poll, return_exceptions=True)


@pytest.mark.asyncio
async def test_retire_without_reserve_reports_degraded_and_keeps_assignment(slot_env):
    repo, _, _, _ = slot_env
    await repo.register_slot("source", "http://10.0.0.1:8080")
    old_generation = await repo.assign_query("source", "q_degraded")
    transferred, degraded = await repo.retire_slot("source")

    assert transferred == []
    assert degraded == ["q_degraded"]
    source = await repo.get_slot("source")
    assert source.health == SlotHealth.DISABLED
    assert source.capacity.assigned_queries == ("q_degraded",)
    assert not await repo.set_health_if_assignment_current(
        "source", "q_degraded", old_generation, SlotHealth.QUARANTINED
    )


@pytest.mark.asyncio
async def test_monotonic_sequence_barrier_rejects_late(slot_env):
    repo, _, _, _ = slot_env
    # First sequence 10 accepted
    assert await repo.record_request_sequence("s1", "q1", 10) is True
    assert await repo.get_request_sequence("s1", "q1") == 10

    # Late response (seq 8) rejected
    assert await repo.record_request_sequence("s1", "q1", 8) is False
    assert await repo.get_request_sequence("s1", "q1") == 10

    # Exact duplicate (seq 10) rejected
    assert await repo.record_request_sequence("s1", "q1", 10) is False

    # Advanced response (seq 15) accepted
    assert await repo.record_request_sequence("s1", "q1", 15) is True
    assert await repo.get_request_sequence("s1", "q1") == 15

    # Independent slot/query has own sequence
    assert await repo.get_request_sequence("s2", "q1") == 0
    assert await repo.record_request_sequence("s2", "q1", 5) is True


@pytest.mark.asyncio
async def test_manual_disable_persists_across_probes(slot_env):
    repo, _, _, _ = slot_env
    await repo.register_slot("s1", "http://10.0.0.1:8080")
    await repo.set_health("s1", SlotHealth.DISABLED, force_manual=True)

    s = await repo.get_slot("s1")
    assert s is not None and s.health == SlotHealth.DISABLED

    # Automated probe signals attempting to mark READY or SUSPECT are ignored
    await repo.set_health("s1", SlotHealth.READY)
    assert (await repo.get_slot("s1")).health == SlotHealth.DISABLED

    await repo.set_health("s1", SlotHealth.SUSPECT)
    assert (await repo.get_slot("s1")).health == SlotHealth.DISABLED

    # Explicit manual override can re-enable slot
    await repo.set_health("s1", SlotHealth.READY, force_manual=True)
    assert (await repo.get_slot("s1")).health == SlotHealth.READY


@pytest.mark.asyncio
async def test_no_cookie_deletion_on_failure_and_transitions(slot_env):
    repo, _, _, db_path = slot_env
    await repo.register_slot("s1", "http://10.0.0.1:8080")
    await repo.bump_session_version("s1", cookie_hash="sha256_cookie_blob")

    slot = await repo.get_slot("s1")
    assert slot is not None and slot.session_version == 2

    # Transitions to QUARANTINED, DISABLED, and unassigning queries
    await repo.set_health("s1", SlotHealth.QUARANTINED)
    await repo.set_health("s1", SlotHealth.DISABLED, force_manual=True)

    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute("SELECT version, cookie_hash FROM session_versions WHERE slot_id = 's1'")
    row = cur.fetchone()
    con.close()

    assert row is not None
    assert row[0] == 2
    assert row[1] == "sha256_cookie_blob"


@pytest.mark.asyncio
async def test_next_probe_at_persistence(slot_env):
    repo, _, _, _ = slot_env
    await repo.register_slot("s1", "http://10.0.0.1:8080")
    probe_ts = "2026-10-23T15:00:00+00:00"
    await repo.set_health("s1", SlotHealth.QUARANTINED, next_probe_at=probe_ts)
    saved = await repo.get_next_probe_at("s1")
    assert saved == probe_ts
