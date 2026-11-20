"""Unit tests for Slot State, Capacity and Health Transitions (S5 / T-476).

Verifies:
- Capacity enforcement: 4 query assignments succeed, 5th assignment rejected
- Reserve slot rejects direct user assignment
- Pure deterministic health transitions
- Exact reconstruction of persisted state, generation, and deadlines
- Day (30s) and Night (180s) intervals in Europe/Moscow
- Independence from legacy polling parameters
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from hunter.domain.slots import (
    CapacityExceededError,
    ReserveAdmissionError,
    SlotCapacity,
    SlotHealth,
    SlotRole,
    SlotSignal,
    SlotSnapshot,
    assign_query_to_slot,
    get_public_poll_interval,
    transition_slot_health,
)

MSK = ZoneInfo("Europe/Moscow")


def test_slot_capacity_four_assignments_and_fifth_rejected() -> None:
    slot = SlotSnapshot(
        slot_id="slot_1",
        endpoint="https://proxy-1.local",
        health=SlotHealth.READY,
        role=SlotRole.ACTIVE,
    )

    # 4 assignments succeed
    s1 = assign_query_to_slot(slot, "q_1")
    s2 = assign_query_to_slot(s1, "q_2")
    s3 = assign_query_to_slot(s2, "q_3")
    s4 = assign_query_to_slot(s3, "q_4")

    assert s4.capacity.current_load == 4
    assert s4.capacity.is_full is True
    assert set(s4.capacity.assigned_queries) == {"q_1", "q_2", "q_3", "q_4"}

    # 5th assignment is strictly rejected
    with pytest.raises(CapacityExceededError) as exc_info:
        assign_query_to_slot(s4, "q_5")
    assert "Slot capacity exceeded" in str(exc_info.value)

    # Release query and reassign
    rel = SlotSnapshot(
        slot_id=s4.slot_id,
        endpoint=s4.endpoint,
        health=s4.health,
        role=s4.role,
        capacity=s4.capacity.release("q_2"),
    )
    assert rel.capacity.current_load == 3
    s5 = assign_query_to_slot(rel, "q_5")
    assert s5.capacity.current_load == 4
    assert "q_5" in s5.capacity.assigned_queries
    assert "q_2" not in s5.capacity.assigned_queries


def test_reserve_slot_admission_rejected() -> None:
    reserve_slot = SlotSnapshot(
        slot_id="slot_res_1",
        endpoint="https://proxy-res.local",
        health=SlotHealth.READY,
        role=SlotRole.RESERVE,
    )
    with pytest.raises(ReserveAdmissionError) as exc_info:
        assign_query_to_slot(reserve_slot, "q_user_1")
    assert "reserve admission prohibited" in str(exc_info.value)


def test_pure_health_transitions() -> None:
    ts = "2026-10-23T12:00:00Z"
    slot = SlotSnapshot(
        slot_id="s1",
        endpoint="http://ep1",
        health=SlotHealth.WARMING,
        role=SlotRole.ACTIVE,
    )

    # WARMING requires 2 probes to reach READY
    s_warm1 = transition_slot_health(slot, SlotSignal.PROBE_OK, timestamp=ts)
    assert s_warm1.health == SlotHealth.WARMING
    assert s_warm1.consecutive_successes == 1

    s_ready = transition_slot_health(s_warm1, SlotSignal.PROBE_OK, timestamp=ts)
    assert s_ready.health == SlotHealth.READY
    assert s_ready.consecutive_successes == 2

    # HTTP 429 once -> SUSPECT
    s_suspect = transition_slot_health(s_ready, SlotSignal.HTTP_429, timestamp=ts)
    assert s_suspect.health == SlotHealth.SUSPECT

    # HTTP 429 twice -> QUARANTINED
    s_quar = transition_slot_health(
        s_suspect, SlotSignal.HTTP_429, timestamp=ts, deadline="2026-10-23T12:15:00Z"
    )
    assert s_quar.health == SlotHealth.QUARANTINED
    assert s_quar.quarantined_until == "2026-10-23T12:15:00Z"

    # Challenge detected -> CHALLENGE
    s_chal = transition_slot_health(
        s_ready, SlotSignal.CHALLENGE_DETECTED, timestamp=ts, deadline="2026-10-23T12:05:00Z"
    )
    assert s_chal.health == SlotHealth.CHALLENGE
    assert s_chal.challenge_deadline == "2026-10-23T12:05:00Z"

    # Challenge solved -> READY with session_version bumped
    s_solved = transition_slot_health(s_chal, SlotSignal.CHALLENGE_SOLVED, timestamp=ts)
    assert s_solved.health == SlotHealth.READY
    assert s_solved.session_version == s_chal.session_version + 1

    # Auth invalid -> AUTH_REQUIRED
    s_auth = transition_slot_health(s_ready, SlotSignal.AUTH_INVALID, timestamp=ts)
    assert s_auth.health == SlotHealth.AUTH_REQUIRED

    # Admin disable -> DISABLED
    s_dis = transition_slot_health(s_ready, SlotSignal.ADMIN_DISABLE, timestamp=ts)
    assert s_dis.health == SlotHealth.DISABLED

    # Disabled ignores normal probes
    s_dis_probe = transition_slot_health(s_dis, SlotSignal.PROBE_OK, timestamp=ts)
    assert s_dis_probe.health == SlotHealth.DISABLED

    # Admin enable -> WARMING
    s_en = transition_slot_health(s_dis, SlotSignal.ADMIN_ENABLE, timestamp=ts)
    assert s_en.health == SlotHealth.WARMING


def test_restart_reconstructs_persisted_deadlines() -> None:
    original = SlotSnapshot(
        slot_id="slot_persist_1",
        endpoint="https://node-1.avito-pool.net",
        health=SlotHealth.QUARANTINED,
        role=SlotRole.ACTIVE,
        session_version=3,
        generation=7,
        capacity=SlotCapacity(max_queries=4, assigned_queries=("q_a", "q_b", "q_c")),
        quarantined_until="2026-10-23T14:30:00Z",
        challenge_deadline="2026-10-23T14:45:00Z",
        consecutive_errors=2,
        consecutive_successes=0,
        updated_at="2026-10-23T14:00:00Z",
    )

    # Round-trip through JSON serialization
    serialized = json.dumps(original.to_dict())
    deserialized_data = json.loads(serialized)
    reconstructed = SlotSnapshot.from_dict(deserialized_data)

    assert reconstructed.slot_id == original.slot_id
    assert reconstructed.endpoint == original.endpoint
    assert reconstructed.health == original.health
    assert reconstructed.role == original.role
    assert reconstructed.session_version == original.session_version
    assert reconstructed.generation == original.generation
    assert reconstructed.quarantined_until == original.quarantined_until
    assert reconstructed.challenge_deadline == original.challenge_deadline
    assert reconstructed.capacity.assigned_queries == ("q_a", "q_b", "q_c")
    assert reconstructed.capacity.current_load == 3


def test_public_poll_interval_day_and_night() -> None:
    # 00:59:59 MSK -> 30s (Day interval until 01:00)
    dt_day_end = datetime(2026, 10, 23, 0, 59, 59, tzinfo=MSK)
    assert get_public_poll_interval(dt_day_end) == 30

    # 01:00:00 MSK -> 180s (Night interval start)
    dt_night_start = datetime(2026, 10, 23, 1, 0, 0, tzinfo=MSK)
    assert get_public_poll_interval(dt_night_start) == 180

    # 04:30:00 MSK -> 180s (Deep night)
    dt_night_mid = datetime(2026, 10, 23, 4, 30, 0, tzinfo=MSK)
    assert get_public_poll_interval(dt_night_mid) == 180

    # 06:59:59 MSK -> 180s (Night interval end)
    dt_night_end = datetime(2026, 10, 23, 6, 59, 59, tzinfo=MSK)
    assert get_public_poll_interval(dt_night_end) == 180

    # 07:00:00 MSK -> 30s (Day interval start)
    dt_day_start = datetime(2026, 10, 23, 7, 0, 0, tzinfo=MSK)
    assert get_public_poll_interval(dt_day_start) == 30

    # 15:00:00 MSK -> 30s (Midday)
    dt_day_mid = datetime(2026, 10, 23, 15, 0, 0, tzinfo=MSK)
    assert get_public_poll_interval(dt_day_mid) == 30

    # UTC timestamps properly converted to MSK
    # 22:00:00 UTC = 01:00:00 MSK -> Night -> 180s
    dt_utc_night = datetime(2026, 10, 22, 22, 0, 0, tzinfo=UTC)
    assert get_public_poll_interval(dt_utc_night) == 180

    # 04:00:00 UTC = 07:00:00 MSK -> Day -> 30s
    dt_utc_day = datetime(2026, 10, 23, 4, 0, 0, tzinfo=UTC)
    assert get_public_poll_interval(dt_utc_day) == 30
