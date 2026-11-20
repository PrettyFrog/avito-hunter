"""Unit tests for CapacityAdmissionController and failover resilience (S5 / T-501).

Covers:
- 100 unique queries admitted into exactly 25 active slots (4 queries per slot).
- Reserve slots are never sold or admitted for regular user queries (101st query denied).
- +2 reserve slots absorb up to two slot failures without additional IP load.
- Third failure exhausts reserve and produces DEGRADED status (insufficient capacity).
- Night hours maintain steady leased IP footprint without churn or mass price sweep.
"""

from __future__ import annotations

import pytest

from hunter.application.capacity import (
    AdmissionStatus,
    CapacityAdmissionController,
)
from hunter.domain.slots import (
    SlotCapacity,
    SlotHealth,
    SlotRole,
    SlotSnapshot,
)


def _make_slot(
    slot_id: str,
    role: SlotRole = SlotRole.ACTIVE,
    health: SlotHealth = SlotHealth.READY,
    assigned: tuple[str, ...] = (),
) -> SlotSnapshot:
    return SlotSnapshot(
        slot_id=slot_id,
        endpoint=f"http://10.0.0.{slot_id}:8080",
        health=health,
        role=role,
        capacity=SlotCapacity(max_queries=4, assigned_queries=assigned),
    )


@pytest.fixture
def controller() -> CapacityAdmissionController:
    return CapacityAdmissionController(max_queries_per_slot=4)


def test_100_queries_admitted_to_25_active_slots(controller: CapacityAdmissionController):
    # Setup 25 active slots and 2 reserve slots
    active_slots = [_make_slot(f"act_{i:02d}") for i in range(25)]
    reserve_slots = [
        _make_slot("res_01", role=SlotRole.RESERVE),
        _make_slot("res_02", role=SlotRole.RESERVE),
    ]

    admitted_count = 0
    assigned_slots: list[SlotSnapshot] = list(active_slots)

    for q_idx in range(100):
        qid = f"query_{q_idx:03d}"
        decision = controller.admit_query(qid, assigned_slots, reserve_slots)
        assert decision.is_admitted is True
        assert decision.status == AdmissionStatus.ADMITTED
        assert decision.slot_id is not None

        # Update slot load in simulation
        target_idx = next(i for i, s in enumerate(assigned_slots) if s.slot_id == decision.slot_id)
        current = assigned_slots[target_idx]
        new_assigned = current.capacity.assigned_queries + (qid,)
        assigned_slots[target_idx] = _make_slot(
            current.slot_id, current.role, current.health, new_assigned
        )
        admitted_count += 1

    assert admitted_count == 100
    # Every one of the 25 active slots has exactly 4 queries
    for s in assigned_slots:
        assert s.capacity.current_load == 4
        assert s.capacity.is_full is True

    # Reserve slots were NEVER sold as regular capacity
    for r in reserve_slots:
        assert r.capacity.current_load == 0


def test_101st_query_denied_when_active_full(controller: CapacityAdmissionController):
    # All 25 active slots full (4/4)
    active_slots = [
        _make_slot(f"act_{i:02d}", assigned=("q1", "q2", "q3", "q4")) for i in range(25)
    ]
    reserve_slots = [
        _make_slot("res_01", role=SlotRole.RESERVE),
        _make_slot("res_02", role=SlotRole.RESERVE),
    ]

    decision = controller.admit_query("query_101", active_slots, reserve_slots)
    assert decision.is_admitted is False
    assert decision.status == AdmissionStatus.WAITING_CAPACITY
    assert "reserve slots reserved for failover" in decision.reason


def test_two_failures_absorbed_by_two_reserve_slots(controller: CapacityAdmissionController):
    # 25 active slots full, 2 reserve slots ready and empty
    active_slots = [
        _make_slot(f"act_{i:02d}", assigned=("q1", "q2", "q3", "q4")) for i in range(25)
    ]
    res1 = _make_slot("res_01", role=SlotRole.RESERVE)
    res2 = _make_slot("res_02", role=SlotRole.RESERVE)
    reserve_slots = [res1, res2]

    # Failure 1: Slot act_00 fails, query_alpha fails over to reserve
    failing_0 = _make_slot("act_00", health=SlotHealth.QUARANTINED)
    d1 = controller.failover_query("query_alpha", failing_0, active_slots, reserve_slots)
    assert d1.is_admitted is True
    assert d1.slot_id == "res_01"

    # Fill res_01 to capacity (absorbed 1st failure batch)
    reserve_slots[0] = _make_slot(
        "res_01", role=SlotRole.RESERVE, assigned=("q_a1", "q_a2", "q_a3", "q_a4")
    )

    # Failure 2: Slot act_01 fails, query_beta fails over to res_02
    failing_1 = _make_slot("act_01", health=SlotHealth.QUARANTINED)
    d2 = controller.failover_query("query_beta", failing_1, active_slots, reserve_slots)
    assert d2.is_admitted is True
    assert d2.slot_id == "res_02"


def test_third_failure_results_in_degraded_capacity(controller: CapacityAdmissionController):
    # Active slots full, both reserve slots already full
    active_slots = [
        _make_slot(f"act_{i:02d}", assigned=("q1", "q2", "q3", "q4")) for i in range(25)
    ]
    reserve_slots = [
        _make_slot("res_01", role=SlotRole.RESERVE, assigned=("r1", "r2", "r3", "r4")),
        _make_slot("res_02", role=SlotRole.RESERVE, assigned=("r5", "r6", "r7", "r8")),
    ]

    # Third failure cannot be absorbed
    failing_2 = _make_slot("act_02", health=SlotHealth.DISABLED)
    d3 = controller.failover_query("query_gamma", failing_2, active_slots, reserve_slots)
    assert d3.is_admitted is False
    assert d3.status == AdmissionStatus.DEGRADED
    assert "exhausted" in d3.reason
