"""Slot health counter persistence and snapshot creation via SQLite runtime (T-973)."""

from __future__ import annotations

from typing import Any

from hunter.domain.slots import SlotCapacity, SlotHealth, SlotRole, SlotSnapshot


def slot_runtime_keys(slot_id: str) -> tuple[str, str, str]:
    return f"slot_err:{slot_id}", f"slot_succ:{slot_id}", f"slot_dead:{slot_id}"


async def load_slot_runtime_counters(c: Any, slot_id: str) -> tuple[int, int, str | None]:
    ke, ks, kd = slot_runtime_keys(slot_id)
    cur = await c.execute("SELECT key, value FROM runtime WHERE key IN (?, ?, ?)", (ke, ks, kd))
    err, succ, dead = 0, 0, None
    for k, v in await cur.fetchall():
        if k == ke:
            try: err = max(0, int(v))
            except (ValueError, TypeError): pass
        elif k == ks:
            try: succ = max(0, int(v))
            except (ValueError, TypeError): pass
        elif k == kd:
            dead = str(v) if v else None
    return err, succ, dead


async def save_slot_runtime_counters(
    c: Any, slot_id: str, errors: int | None = None, successes: int | None = None, deadline: str | None = None,
) -> None:
    ke, ks, kd = slot_runtime_keys(slot_id)
    pairs = [(k, str(v)) for k, v in ((ke, errors), (ks, successes), (kd, deadline)) if v is not None]
    for k, v in pairs:
        await c.execute(
            "INSERT INTO runtime (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (k, v),
        )


def build_slot_snapshot_from_row(
    row: tuple[Any, ...], assigned: tuple[str, ...], err: int = 0, succ: int = 0, dead: str | None = None,
) -> SlotSnapshot:
    role = SlotRole(str(row[7]))
    health = SlotHealth(str(row[2])) if str(row[2]) in [h.value for h in SlotHealth] else SlotHealth.READY
    return SlotSnapshot(
        slot_id=str(row[0]), endpoint=str(row[1]), health=health, role=role,
        session_version=int(row[6]), generation=int(row[4]), capacity=SlotCapacity(assigned_queries=assigned),
        quarantined_until=str(row[3]) if row[3] else None, challenge_deadline=dead,
        consecutive_errors=err, consecutive_successes=succ, updated_at=str(row[5]),
    )


async def execute_quarantine_recovery_cas(
    writer: Any, slot_id: str, expected_health: SlotHealth, expected_quarantine: str | None, expected_gen: int, now_ts: str,
) -> bool:
    async def _tx(c: Any) -> bool:
        cur = await c.execute(
            """
            UPDATE slots SET health_state = 'READY', quarantine_until = NULL, updated_at = ?
            WHERE id = ? AND health_state = ? AND active_generation = ?
              AND (quarantine_until = ? OR (quarantine_until IS NULL AND ? IS NULL))
              AND (quarantine_until IS NULL OR quarantine_until <= ?)
            """,
            (now_ts, slot_id, expected_health.value, expected_gen, expected_quarantine, expected_quarantine, now_ts),
        )
        if cur.rowcount == 1:
            await save_slot_runtime_counters(c, slot_id, deadline="")
        return cur.rowcount == 1

    return await writer.submit(_tx)
