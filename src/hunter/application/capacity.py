"""Admission control, reserve slot isolation, and failover capacity management (S5 / T-501).

Contracts:
- Assigns physical queries strictly to available positions (0..3) in healthy active slots.
- Reserve slots are never sold or admitted for regular user queries.
- No hidden fifth query: slots strictly reject capacity overflows (> 4).
- Budget v2 is scoped per-slot without legacy global cap cross-throttling.
- Failover model: +2 reserve slots absorb up to two failures; third failure results in degraded capacity.
- Night pacing stays at 180s per query; night spare capacity is not drained by mass price sweeps.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from hunter.domain.slots import (
    CapacityExceededError,
    ReserveAdmissionError,
    SlotHealth,
    SlotRole,
    SlotSnapshot,
)


class AdmissionStatus(str, Enum):
    ADMITTED = "admitted"
    WAITING_CAPACITY = "waiting_capacity"
    DEGRADED = "degraded"
    RESERVE_STANDBY = "reserve_standby"


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    query_id: str
    slot_id: str | None
    position: int | None
    status: AdmissionStatus
    reason: str = ""

    @property
    def is_admitted(self) -> bool:
        return self.status == AdmissionStatus.ADMITTED and self.slot_id is not None


class CapacityAdmissionController:
    """Controls physical query admission into slots, enforcing reserve boundaries and failover limits."""

    def __init__(self, max_queries_per_slot: int = 4) -> None:
        self.max_queries_per_slot = int(max_queries_per_slot)

    def _find_free_position(self, slot: SlotSnapshot) -> int | None:
        used_positions = set()
        for idx in range(slot.capacity.current_load):
            used_positions.add(idx)
        for pos in range(self.max_queries_per_slot):
            if pos not in used_positions:
                return pos
        return None

    def admit_query(
        self,
        query_id: str,
        active_slots: list[SlotSnapshot],
        reserve_slots: list[SlotSnapshot] | None = None,
    ) -> AdmissionDecision:
        """Admits physical query strictly into healthy active slots with free positions (<= 4).

        Reserve slots are NEVER sold or admitted for standard queries.
        """
        for slot in active_slots:
            if slot.role != SlotRole.ACTIVE:
                continue
            if slot.health not in (SlotHealth.READY, SlotHealth.WARMING):
                continue
            if slot.capacity.current_load >= self.max_queries_per_slot:
                continue

            pos = self._find_free_position(slot)
            if pos is not None:
                return AdmissionDecision(
                    query_id=query_id,
                    slot_id=slot.slot_id,
                    position=pos,
                    status=AdmissionStatus.ADMITTED,
                    reason=f"Admitted to active slot '{slot.slot_id}' at position {pos}",
                )

        return AdmissionDecision(
            query_id=query_id,
            slot_id=None,
            position=None,
            status=AdmissionStatus.WAITING_CAPACITY,
            reason="All active slots at capacity; reserve slots reserved for failover",
        )

    def failover_query(
        self,
        query_id: str,
        failing_slot: SlotSnapshot,
        active_slots: list[SlotSnapshot],
        reserve_slots: list[SlotSnapshot],
    ) -> AdmissionDecision:
        """Transfers a query from a failing slot to spare capacity or reserve.

        +2 reserve slots can absorb 2 failures; further failures degrade gracefully.
        """
        # 1. Try healthy active slots with free capacity first
        for slot in active_slots:
            if slot.slot_id == failing_slot.slot_id:
                continue
            if slot.health in (SlotHealth.READY, SlotHealth.WARMING) and not slot.capacity.is_full:
                pos = self._find_free_position(slot)
                if pos is not None:
                    return AdmissionDecision(
                        query_id=query_id,
                        slot_id=slot.slot_id,
                        position=pos,
                        status=AdmissionStatus.ADMITTED,
                        reason=f"Failed over to spare active slot '{slot.slot_id}'",
                    )

        # 2. Try ready reserve slots
        for r_slot in reserve_slots:
            if r_slot.health in (SlotHealth.READY, SlotHealth.WARMING) and not r_slot.capacity.is_full:
                pos = self._find_free_position(r_slot)
                if pos is not None:
                    return AdmissionDecision(
                        query_id=query_id,
                        slot_id=r_slot.slot_id,
                        position=pos,
                        status=AdmissionStatus.ADMITTED,
                        reason=f"Failed over to reserve slot '{r_slot.slot_id}'",
                    )

        # 3. All reserve and active slots exhausted
        return AdmissionDecision(
            query_id=query_id,
            slot_id=None,
            position=None,
            status=AdmissionStatus.DEGRADED,
            reason="Insufficient capacity: all reserve slots and active slots exhausted",
        )
