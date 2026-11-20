"""Generation-fenced slot disable, enable, and retirement operations."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from hunter.domain.slots import SlotCapacityError, SlotHealth, SlotRole, SlotSnapshot


class SlotLifecycleMixin:
    """Lifecycle methods mixed into SlotRepository without widening its module."""

    async def disable_slot(self, slot_id: str) -> SlotSnapshot:
        now = datetime.now(UTC).isoformat()

        async def _tx(conn: Any) -> None:
            row = await (
                await conn.execute(
                    "SELECT active_generation, health_state, "
                    "COALESCE((SELECT max(generation) FROM assignments WHERE slot_id = ?), 0) "
                    "FROM slots WHERE id = ?",
                    (slot_id, slot_id),
                )
            ).fetchone()
            if row is None:
                raise SlotCapacityError(f"Slot '{slot_id}' not found")
            if row[1] == SlotHealth.DISABLED.value:
                return
            assignments = await (
                await conn.execute(
                    "SELECT id, generation FROM assignments WHERE slot_id = ? AND active = 1 "
                    "ORDER BY position",
                    (slot_id,),
                )
            ).fetchall()
            generation = max(
                [int(row[0]), int(row[2]), *(int(item[1]) for item in assignments)]
            )
            for assignment_id, _old_generation in assignments:
                generation += 1
                await conn.execute(
                    "UPDATE assignments SET generation = ?, updated_at = ? "
                    "WHERE id = ? AND active = 1",
                    (generation, now, assignment_id),
                )
            generation += 1
            await conn.execute(
                "UPDATE slots SET health_state = ?, quarantine_until = NULL, "
                "active_generation = ?, updated_at = ? WHERE id = ?",
                (SlotHealth.DISABLED.value, generation, now, slot_id),
            )

        await self.writer.submit(_tx)
        slot = await self.get_slot(slot_id)
        if slot is None:
            raise RuntimeError(f"Slot {slot_id} disappeared after disable")
        return slot

    async def enable_slot(self, slot_id: str) -> SlotSnapshot:
        now = datetime.now(UTC).isoformat()

        async def _tx(conn: Any) -> None:
            cur = await conn.execute(
                "UPDATE slots SET health_state = ?, quarantine_until = NULL, "
                "active_generation = active_generation + 1, updated_at = ? "
                "WHERE id = ? AND health_state = ?",
                (SlotHealth.WARMING.value, now, slot_id, SlotHealth.DISABLED.value),
            )
            if cur.rowcount == 0:
                exists = await (
                    await conn.execute("SELECT 1 FROM slots WHERE id = ?", (slot_id,))
                ).fetchone()
                if exists is None:
                    raise SlotCapacityError(f"Slot '{slot_id}' not found")

        await self.writer.submit(_tx)
        slot = await self.get_slot(slot_id)
        if slot is None:
            raise RuntimeError(f"Slot {slot_id} disappeared after enable")
        return slot

    async def retire_slot(self, slot_id: str) -> tuple[list[str], list[str]]:
        """Disable and fence a slot, transfer what fits, and report degraded queries."""
        disabled = await self.disable_slot(slot_id)
        transferred: list[str] = []
        degraded: list[str] = []
        for query_id in disabled.capacity.assigned_queries:
            moved = False
            for spare in await self.list_slots(role=SlotRole.RESERVE):
                if spare.health != SlotHealth.READY or spare.capacity.is_full:
                    continue
                try:
                    await self.transfer_to_spare(slot_id, spare.slot_id, query_id)
                except SlotCapacityError:
                    continue
                transferred.append(query_id)
                moved = True
                break
            if not moved:
                degraded.append(query_id)
        return transferred, degraded
