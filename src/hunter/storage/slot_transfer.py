"""Atomic slot assignment transfer to a ready spare slot."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from hunter.domain.slots import CapacityExceededError, SlotCapacityError, SlotHealth
from hunter.storage.writer import SQLiteWriterActor


async def transfer_to_spare(
    writer: SQLiteWriterActor, from_slot_id: str, to_slot_id: str, query_id: str
) -> int:
    """Move one active query to a ready spare in a single writer transaction."""
    if from_slot_id == to_slot_id:
        raise SlotCapacityError("Source and target slots must differ")
    now_ts = datetime.now(UTC).isoformat()

    async def _tx(conn: Any) -> int:
        return await _transfer_assignment(conn, from_slot_id, to_slot_id, query_id, now_ts)

    return await writer.submit(_tx)


async def _transfer_assignment(
    conn: Any, source_id: str, target_id: str, query_id: str, now_ts: str
) -> int:
    source, target = await _load_slot_generations(conn, source_id, target_id)
    if not source or not target:
        raise SlotCapacityError("Source or spare slot not found")
    if target[1] != SlotHealth.READY.value:
        raise SlotCapacityError(f"Spare slot '{target_id}' is not ready")

    assignment_id = await _load_source_assignment(conn, source_id, query_id)
    if not assignment_id:
        raise SlotCapacityError("Query is no longer assigned to source slot")
    await _ensure_no_other_owner(conn, source_id, query_id)
    position = await _find_available_position(conn, target_id)
    generation = int(target[0]) + 1

    await _advance_generations(conn, source_id, target_id, now_ts)
    await _deactivate_assignment(conn, assignment_id, now_ts)
    await _insert_target_assignment(conn, target_id, query_id, position, generation, now_ts)
    return generation


async def _load_slot_generations(conn: Any, source_id: str, target_id: str) -> tuple[Any, Any]:
    source = await (
        await conn.execute("SELECT active_generation FROM slots WHERE id = ?", (source_id,))
    ).fetchone()
    target = await (
        await conn.execute(
            "SELECT active_generation, health_state FROM slots WHERE id = ?", (target_id,)
        )
    ).fetchone()
    return source, target


async def _load_source_assignment(conn: Any, source_id: str, query_id: str) -> Any:
    row = await (
        await conn.execute(
            "SELECT id FROM assignments WHERE slot_id = ? AND query_id = ? AND active = 1",
            (source_id, query_id),
        )
    ).fetchone()
    return row[0] if row else None


async def _ensure_no_other_owner(conn: Any, source_id: str, query_id: str) -> None:
    row = await (
        await conn.execute(
            "SELECT 1 FROM assignments WHERE query_id = ? AND slot_id != ? AND active = 1 LIMIT 1",
            (query_id, source_id),
        )
    ).fetchone()
    if row:
        raise SlotCapacityError("Query already has another active slot")


async def _find_available_position(conn: Any, target_id: str) -> int:
    rows = await (
        await conn.execute(
            "SELECT position FROM assignments WHERE slot_id = ? AND active = 1",
            (target_id,),
        )
    ).fetchall()
    occupied = {int(row[0]) for row in rows}
    position = next((pos for pos in range(4) if pos not in occupied), None)
    if position is None:
        raise CapacityExceededError(f"Spare slot '{target_id}' is full")
    return position


async def _advance_generations(conn: Any, source_id: str, target_id: str, now_ts: str) -> None:
    await conn.execute(
        "UPDATE slots SET active_generation = active_generation + 1, updated_at = ? "
        "WHERE id IN (?, ?)",
        (now_ts, source_id, target_id),
    )


async def _deactivate_assignment(conn: Any, assignment_id: str, now_ts: str) -> None:
    await conn.execute(
        "UPDATE assignments SET active = 0, updated_at = ? WHERE id = ?",
        (now_ts, assignment_id),
    )


async def _insert_target_assignment(
    conn: Any,
    target_id: str,
    query_id: str,
    position: int,
    generation: int,
    now_ts: str,
) -> None:
    await conn.execute(
        """INSERT INTO assignments (
           id, slot_id, query_id, position, generation, active, updated_at
           )
           VALUES (?, ?, ?, ?, ?, 1, ?)
           ON CONFLICT(slot_id, position) DO UPDATE SET
           id = excluded.id, query_id = excluded.query_id, generation = excluded.generation,
           active = 1, updated_at = excluded.updated_at""",
        (
            f"asgn_{target_id}_{query_id}_{generation}",
            target_id,
            query_id,
            position,
            generation,
            now_ts,
        ),
    )
