"""Slot supervisor managing health signals, quarantine transitions, and spare failover (S5 / T-482).

Contracts:
- Protocol: persists state -> cancels old -> increments generation -> assigns spare.
- No spare capacity results in DEGRADED status; never overloads neighboring slots (> 4).
- Cookies are strictly isolated per IP/slot and never copied across endpoints during failover.
- Up to 2 failures absorbed by reserve slots in a 25+2 model; 3rd failure degrades gracefully.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from hunter.application.capacity import CapacityAdmissionController
from hunter.domain.recovery_policy import saturated_deadline
from hunter.domain.slots import (
    SlotHealth,
    SlotRole,
    SlotSignal,
    SlotSnapshot,
    transition_slot_health,
)
from hunter.storage.slot_state import SlotRepository


@dataclass(frozen=True, slots=True)
class FailoverOutcome:
    success: bool
    from_slot_id: str
    transferred_queries: list[str]
    untransferred_queries: list[str]
    target_slot_id: str | None
    new_health: SlotHealth
    new_generation: int | None
    status: str
    reason: str = ""


def _compute_backoff(signal: SlotSignal, errors: int, requested: float | None) -> float:
    if requested is not None and requested != 3600.0:
        return max(0.0, requested)
    if signal in (SlotSignal.HTTP_429, SlotSignal.HTTP_403, SlotSignal.CHALLENGE_DETECTED):
        tier = (62.0, 120.0, 300.0)
        return tier[min(max(0, errors), len(tier) - 1)]
    if signal == SlotSignal.TIMEOUT:
        return 15.0 if errors <= 1 else 45.0
    return requested if requested is not None else 3600.0


class SlotSupervisor:
    """Supervises slot health transitions, workers cancellation, and capacity failover."""

    def __init__(
        self,
        slot_repo: SlotRepository,
        admission_ctrl: CapacityAdmissionController | None = None,
        workers: dict[str, Any] | None = None,
    ) -> None:
        self.slot_repo = slot_repo
        self.admission_ctrl = admission_ctrl or CapacityAdmissionController(max_queries_per_slot=4)
        self.workers = workers if workers is not None else {}
        self.on_assignment_changed: Any = None
        self._rebalance_tick: int = 100

    async def _cancel_worker(self, slot_id: str) -> None:
        worker = self.workers.get(slot_id)
        if worker and hasattr(worker, "stop"):
            await worker.stop()

    async def check_quarantined_slots(self, now_ts: str | None = None) -> list[str]:
        """Recovers slots whose quarantine has expired back to READY."""
        now = datetime.fromisoformat(now_ts) if now_ts else datetime.now(UTC)
        now_iso = now.isoformat()
        recovered: list[str] = []
        slots = await self.slot_repo.list_slots()
        for slot in slots:
            if slot.health in (SlotHealth.QUARANTINED, SlotHealth.CHALLENGE, SlotHealth.SUSPECT):
                deadline = slot.quarantined_until or slot.challenge_deadline
                if deadline:
                    try:
                        d_dt = datetime.fromisoformat(deadline)
                        if d_dt.tzinfo is None:
                            d_dt = d_dt.replace(tzinfo=UTC)
                        if now >= d_dt:
                            if hasattr(self.slot_repo, "recover_quarantined_slot_cas"):
                                cas_ok = await self.slot_repo.recover_quarantined_slot_cas(
                                    slot.slot_id, slot.health, slot.quarantined_until, slot.generation, now_iso,
                                )
                            else:
                                cur = getattr(self.slot_repo, "current", None)
                                if cur and (cur.health != slot.health or cur.quarantined_until != slot.quarantined_until or cur.generation != slot.generation):
                                    continue
                                await self.slot_repo.set_health(slot.slot_id, SlotHealth.READY, quarantined_until=None)
                                cas_ok = True
                            if cas_ok:
                                recovered.append(slot.slot_id)
                                if self.on_assignment_changed:
                                    for qid in slot.capacity.assigned_queries:
                                        await self.on_assignment_changed(qid)
                    except Exception:
                        pass
        self._rebalance_tick += 1
        if recovered or self._rebalance_tick >= 100:
            self._rebalance_tick = 0
            await self.rebalance_active_slots()
        return recovered

    async def rebalance_active_slots(self) -> list[str]:
        """Rebalances query assignments across ready active slots when load skew > 1."""
        rebalanced: list[str] = []
        all_slots = await self.slot_repo.list_slots()
        active_slots = [s for s in all_slots if getattr(s, "role", None) == SlotRole.ACTIVE]
        ready = [s for s in active_slots if s.health in (SlotHealth.READY, SlotHealth.WARMING)]
        if len(ready) < 2:
            return rebalanced
        ready.sort(key=lambda s: s.capacity.current_load)
        while ready[-1].capacity.current_load - ready[0].capacity.current_load > 1:
            donor, receiver = ready[-1], ready[0]
            if not donor.capacity.assigned_queries:
                break
            qid = donor.capacity.assigned_queries[-1]
            try:
                await self.slot_repo.transfer_to_spare(
                    from_slot_id=donor.slot_id,
                    to_spare_slot_id=receiver.slot_id,
                    query_id=qid,
                )
                if self.on_assignment_changed:
                    await self.on_assignment_changed(qid)
                rebalanced.append(qid)
            except Exception:
                break
            all_slots = await self.slot_repo.list_slots()
            active_slots = [s for s in all_slots if getattr(s, "role", None) == SlotRole.ACTIVE]
            ready = [s for s in active_slots if s.health in (SlotHealth.READY, SlotHealth.WARMING)]
            if len(ready) < 2:
                break
            ready.sort(key=lambda s: s.capacity.current_load)
        return rebalanced


    async def _failover_assigned_queries(
        self, slot: SlotSnapshot
    ) -> tuple[list[str], list[str], str | None, int | None]:
        active_slots = await self.slot_repo.list_slots(role=SlotRole.ACTIVE)
        reserve_slots = await self.slot_repo.list_slots(role=SlotRole.RESERVE)

        transferred: list[str] = []
        untransferred: list[str] = []
        last_target_id: str | None = None
        last_gen: int | None = None

        for qid in slot.capacity.assigned_queries:
            decision = self.admission_ctrl.failover_query(
                query_id=qid,
                failing_slot=slot,
                active_slots=active_slots,
                reserve_slots=reserve_slots,
            )
            if decision.is_admitted and decision.slot_id:
                last_target_id = decision.slot_id
                last_gen = await self.slot_repo.transfer_to_spare(
                    from_slot_id=slot.slot_id,
                    to_spare_slot_id=last_target_id,
                    query_id=qid,
                )
                if self.on_assignment_changed:
                    await self.on_assignment_changed(qid)
                transferred.append(qid)
                reserve_slots = await self.slot_repo.list_slots(role=SlotRole.RESERVE)
                active_slots = await self.slot_repo.list_slots(role=SlotRole.ACTIVE)
            else:
                untransferred.append(qid)

        return transferred, untransferred, last_target_id, last_gen

    async def handle_slot_signal(
        self, slot_id: str, signal: SlotSignal, quarantine_duration_sec: float = 3600.0,
        *, query_id: str | None = None, assignment_generation: int | None = None,
    ) -> FailoverOutcome:
        """Executes full failover protocol on slot health signal."""
        if (query_id is None) != (assignment_generation is None):
            return self._stale_signal(slot_id, SlotHealth.READY)
        slot = await self.slot_repo.get_slot(slot_id)
        if not slot:
            return FailoverOutcome(
                success=False, from_slot_id=slot_id, transferred_queries=[], untransferred_queries=[],
                target_slot_id=None, new_health=SlotHealth.DISABLED, new_generation=None,
                status="slot_not_found", reason=f"Slot '{slot_id}' not found",
            )

        from hunter.domain.recovery_policy import compute_slot_backoff
        duration = compute_slot_backoff(signal, slot.consecutive_errors, quarantine_duration_sec)
        updated_slot = await self._persist_health_signal(
            slot, signal, duration, query_id, assignment_generation,
        )
        if updated_slot is None:
            current = await self.slot_repo.get_slot(slot_id)
            return self._stale_signal(
                slot_id, current.health if current else SlotHealth.DISABLED
            )
        slot = updated_slot
        new_health = slot.health
        if new_health not in (SlotHealth.READY, SlotHealth.WARMING):
            await self._cancel_worker(slot_id)
        elif self.on_assignment_changed:
            for qid in slot.capacity.assigned_queries:
                await self.on_assignment_changed(qid)

        if new_health in (SlotHealth.READY, SlotHealth.WARMING):
            return FailoverOutcome(
                success=True, from_slot_id=slot_id, transferred_queries=[], untransferred_queries=[],
                target_slot_id=None, new_health=new_health, new_generation=slot.generation,
                status="healthy", reason="Slot healthy; no failover needed",
            )

        transferred, untransferred, target_id, target_gen = await self._failover_assigned_queries(slot)
        if untransferred:
            return FailoverOutcome(
                success=False, from_slot_id=slot_id, transferred_queries=transferred,
                untransferred_queries=untransferred, target_slot_id=target_id, new_health=new_health,
                new_generation=target_gen, status="degraded",
                reason="No spare capacity: reserve slots exhausted, queries degraded without overloading neighbors",
            )

        return FailoverOutcome(
            success=True, from_slot_id=slot_id, transferred_queries=transferred,
            untransferred_queries=[], target_slot_id=target_id, new_health=new_health,
            new_generation=target_gen, status="failover_complete",
            reason=f"Transferred {len(transferred)} queries to spare slot '{target_id}'",
        )

    async def _persist_health_signal(
        self,
        slot: SlotSnapshot,
        signal: SlotSignal,
        quarantine_duration_sec: float,
        query_id: str | None,
        assignment_generation: int | None,
    ) -> SlotSnapshot | None:
        now = datetime.now(UTC)
        now_ts = now.isoformat()
        deadline = saturated_deadline(now, max(0.0, quarantine_duration_sec)).isoformat()
        new_slot = transition_slot_health(
            slot, signal, timestamp=now_ts, deadline=deadline
        )
        quarantine_until = new_slot.quarantined_until or new_slot.challenge_deadline
        if query_id is not None and assignment_generation is not None:
            applied = await self.slot_repo.set_health_if_assignment_current(
                slot.slot_id, query_id, assignment_generation, new_slot.health,
                quarantined_until=quarantine_until, challenge_deadline=new_slot.challenge_deadline,
                consecutive_errors=new_slot.consecutive_errors, consecutive_successes=new_slot.consecutive_successes,
            )
            if not applied:
                return None
            return await self.slot_repo.get_slot(slot.slot_id)
        else:
            await self.slot_repo.set_health(
                slot.slot_id, new_slot.health, quarantined_until=quarantine_until,
                challenge_deadline=new_slot.challenge_deadline,
                consecutive_errors=new_slot.consecutive_errors, consecutive_successes=new_slot.consecutive_successes,
            )
        return await self.slot_repo.get_slot(slot.slot_id)

    def _stale_signal(self, slot_id: str, health: SlotHealth) -> FailoverOutcome:
        return FailoverOutcome(
            success=True,
            from_slot_id=slot_id,
            transferred_queries=[],
            untransferred_queries=[],
            target_slot_id=None,
            new_health=health,
            new_generation=None,
            status="stale_signal",
            reason="The originating query assignment is no longer active",
        )
