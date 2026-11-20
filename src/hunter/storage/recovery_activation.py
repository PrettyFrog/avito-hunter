"""Fenced slot activation and immutable profile reference share one transaction."""
from datetime import UTC, datetime
from typing import Any

from hunter.domain.slots import SlotHealth


def _build_activation_tx(
    slot_id: str, exp_gen: int, target_endpoint: str | None, expected_version: int,
    query_id: str | None = None, asg_gen: int | None = None,
    session_slot_id: str | None = None, candidate_filename: str | None = None,
    prepared: Any = None,
) -> Any:
    async def _tx(c: Any) -> bool:
        row = await (await c.execute(
            "SELECT health_state, active_generation, endpoint FROM slots WHERE id = ?", (slot_id,)
        )).fetchone()
        if not row or row[0] == SlotHealth.DISABLED.value or int(row[1]) != exp_gen:
            return False
        if target_endpoint and row[2] != target_endpoint:
            return False
        if query_id is not None and asg_gen is not None:
            asg = await (await c.execute(
                "SELECT 1 FROM assignments WHERE slot_id = ? AND query_id = ? AND generation = ? AND active = 1",
                (slot_id, query_id, int(asg_gen)),
            )).fetchone()
            if not asg:
                return False
        v_row = await (await c.execute("SELECT version FROM session_versions WHERE slot_id = ?", (slot_id,))).fetchone()
        if not v_row or int(v_row[0]) != expected_version:
            return False
        if prepared is not None:
            from hunter.storage.session_reference import read_reference_record
            reference = await read_reference_record(c, session_slot_id or slot_id)
            if reference != prepared.previous_reference:
                return False
            if reference and reference["version"] > expected_version:
                return False
        now_ts = datetime.now(UTC).isoformat()
        await c.execute(
            "UPDATE slots SET health_state = ?, quarantine_until = NULL, updated_at = ? WHERE id = ?",
            (SlotHealth.READY.value, now_ts, slot_id),
        )
        await c.execute(
            "INSERT INTO runtime(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (f"recovery:active:{session_slot_id or slot_id}", str(expected_version)),
        )
        if candidate_filename is not None:
            from hunter.storage.session_reference import publish_reference
            await publish_reference(c, session_slot_id or slot_id, expected_version, candidate_filename,
                                    generation=exp_gen if prepared is not None else 0)
        return True
    return _tx
