"""Persisted slot session state, generation transitions, and request sequence high-water mark.

Contracts:
- Atomic generation bump before query reassignment.
- High-water mark request sequence: rejects late/stale responses.
- Failover transfer to ready spare slot with new generation.
- Manual disable persists across restarts and ignores probe signals.
- No cookie deletion during transitions or failures.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import aiosqlite

from hunter.domain.slots import (
    CapacityExceededError,
    ReserveAdmissionError,
    SlotCapacity,
    SlotCapacityError,
    SlotHealth,
    SlotRole,
    SlotSnapshot,
    validate_slot_id,
    validate_proxy_endpoint,
)
from hunter.storage.connection import StorageConnectionManager
from hunter.storage.slot_lifecycle import SlotLifecycleMixin
from hunter.storage.slot_transfer import transfer_to_spare as transfer_slot_to_spare
from hunter.storage.writer import SQLiteWriterActor


class SlotRepository(SlotLifecycleMixin):
    """Persist slot health, versioned sessions, query assignments, and sequence barriers."""

    def __init__(
        self,
        mgr: StorageConnectionManager,
        writer: SQLiteWriterActor,
        db: Any = None,
    ) -> None:
        self.mgr = mgr
        self.writer = writer
        self.db = db

    def _seq_key(self, slot_id: str, query_id: str) -> str:
        return f"slot_hw:{slot_id}:{query_id}"

    def _probe_key(self, slot_id: str) -> str:
        return f"slot_next_probe:{slot_id}"

    @asynccontextmanager
    async def _read_conn(self) -> AsyncIterator[aiosqlite.Connection]:
        conn = await self.mgr.open_read_connection()
        try:
            yield conn
        finally:
            await self.mgr.close_reader(conn)

    async def register_slot(
        self,
        slot_id: str,
        endpoint: str,
        account_id: str = "",
        cookie_namespace: str = "",
        health: SlotHealth = SlotHealth.READY,
        role: SlotRole = SlotRole.ACTIVE,
    ) -> SlotSnapshot:
        validate_slot_id(slot_id)
        validate_proxy_endpoint(endpoint)
        now_ts = datetime.now(UTC).isoformat()
        ns = cookie_namespace or f"cookie_{slot_id}"

        async def _tx(c: Any) -> None:
            await c.execute(
                """
                INSERT INTO slots (
                    id, endpoint, account_id, cookie_namespace, health_state,
                    active_generation, role, updated_at
                )
                VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    endpoint = excluded.endpoint,
                    account_id = excluded.account_id,
                    cookie_namespace = excluded.cookie_namespace,
                    role = excluded.role,
                    updated_at = excluded.updated_at
                """,
                (slot_id, endpoint, account_id, ns, health.value, role.value, now_ts),
            )
            await c.execute(
                """
                INSERT INTO session_versions (slot_id, version, cookie_hash, updated_at)
                VALUES (?, 1, '', ?)
                ON CONFLICT(slot_id) DO NOTHING
                """,
                (slot_id, now_ts),
            )

        await self.writer.submit(_tx)
        res = await self.get_slot(slot_id)
        if not res:
            raise RuntimeError(f"Failed to load registered slot {slot_id}")
        return res

    async def get_slot(self, slot_id: str) -> SlotSnapshot | None:
        async with self._read_conn() as c:
            cur = await c.execute(
                """
                SELECT s.id, s.endpoint, s.health_state, s.quarantine_until,
                       s.active_generation, s.updated_at,
                       COALESCE(v.version, 1) as session_version, s.role
                FROM slots s
                LEFT JOIN session_versions v ON v.slot_id = s.id
                WHERE s.id = ?
                """,
                (slot_id,),
            )
            row = await cur.fetchone()
            if not row:
                return None

            cur_a = await c.execute(
                "SELECT query_id FROM assignments WHERE slot_id = ? AND active = 1 "
                "ORDER BY position ASC",
                (slot_id,),
            )
            assigned = tuple(str(r[0]) for r in await cur_a.fetchall())
            from hunter.storage.slot_health import build_slot_snapshot_from_row, load_slot_runtime_counters
            c_err, c_succ, c_dead = await load_slot_runtime_counters(c, slot_id)
            return build_slot_snapshot_from_row(row, assigned, c_err, c_succ, c_dead)

    async def list_slots(self, role: SlotRole | None = None) -> list[SlotSnapshot]:
        async with self._read_conn() as c:
            cur = await c.execute("SELECT id FROM slots ORDER BY id ASC")
            rows = await cur.fetchall()

        slots: list[SlotSnapshot] = []
        for r in rows:
            s = await self.get_slot(str(r[0]))
            if s is not None and (role is None or s.role == role):
                slots.append(s)
        return slots

    async def set_health(
        self,
        slot_id: str,
        health: SlotHealth,
        quarantined_until: str | None = None,
        challenge_deadline: str | None = None,
        next_probe_at: str | None = None,
        force_manual: bool = False,
        consecutive_errors: int | None = None,
        consecutive_successes: int | None = None,
    ) -> SlotSnapshot:
        now_ts = datetime.now(UTC).isoformat()
        probe_key = self._probe_key(slot_id)

        async def _tx(c: Any) -> None:
            cur = await c.execute("SELECT health_state FROM slots WHERE id = ?", (slot_id,))
            row = await cur.fetchone()
            if row and row[0] == SlotHealth.DISABLED.value and not force_manual:
                return

            await c.execute(
                "UPDATE slots SET health_state = ?, quarantine_until = ?, updated_at = ? WHERE id = ?",
                (health.value, quarantined_until, now_ts, slot_id),
            )
            from hunter.storage.slot_health import save_slot_runtime_counters
            await save_slot_runtime_counters(
                c, slot_id, errors=consecutive_errors, successes=consecutive_successes, deadline=challenge_deadline,
            )
            if next_probe_at is not None:
                await c.execute(
                    "INSERT INTO runtime (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (probe_key, next_probe_at),
                )

        await self.writer.submit(_tx)
        res = await self.get_slot(slot_id)
        if not res:
            raise RuntimeError(f"Slot {slot_id} not found after health update")
        return res

    async def set_health_if_assignment_current(
        self,
        slot_id: str,
        query_id: str,
        assignment_generation: int,
        health: SlotHealth,
        *,
        quarantined_until: str | None = None,
        challenge_deadline: str | None = None,
        consecutive_errors: int | None = None,
        consecutive_successes: int | None = None,
    ) -> bool:
        """CAS a health signal only while its originating assignment is active."""
        now_ts = datetime.now(UTC).isoformat()

        async def _tx(c: Any) -> bool:
            current = await c.execute(
                "SELECT 1 FROM assignments WHERE slot_id = ? AND query_id = ? AND generation = ? AND active = 1",
                (slot_id, query_id, int(assignment_generation)),
            )
            if await current.fetchone() is None:
                return False
            updated = await c.execute(
                "UPDATE slots SET health_state = ?, quarantine_until = ?, updated_at = ? WHERE id = ? AND health_state != ?",
                (health.value, quarantined_until, now_ts, slot_id, SlotHealth.DISABLED.value),
            )
            if updated.rowcount == 1:
                from hunter.storage.slot_health import save_slot_runtime_counters
                await save_slot_runtime_counters(
                    c, slot_id, errors=consecutive_errors, successes=consecutive_successes, deadline=challenge_deadline,
                )
            return updated.rowcount == 1

        return await self.writer.submit(_tx)

    async def get_next_probe_at(self, slot_id: str) -> str | None:
        key = self._probe_key(slot_id)
        async with self._read_conn() as c:
            cur = await c.execute("SELECT value FROM runtime WHERE key = ?", (key,))
            row = await cur.fetchone()
            return str(row[0]) if row and row[0] else None

    async def recover_quarantined_slot_cas(
        self, slot_id: str, expected_health: SlotHealth, expected_quarantine_until: str | None, expected_generation: int, now_ts: str,
    ) -> bool:
        from hunter.storage.slot_health import execute_quarantine_recovery_cas
        return await execute_quarantine_recovery_cas(
            self.writer, slot_id, expected_health, expected_quarantine_until, expected_generation, now_ts,
        )

    async def bump_generation(self, slot_id: str) -> int:
        now_ts = datetime.now(UTC).isoformat()

        async def _tx(c: Any) -> int:
            await c.execute(
                "UPDATE slots SET active_generation = active_generation + 1, updated_at = ? "
                "WHERE id = ?",
                (now_ts, slot_id),
            )
            cur = await c.execute("SELECT active_generation FROM slots WHERE id = ?", (slot_id,))
            row = await cur.fetchone()
            return int(row[0]) if row else 1

        return await self.writer.submit(_tx)

    async def bump_session_version(self, slot_id: str, cookie_hash: str = "") -> int:
        now_ts = datetime.now(UTC).isoformat()

        async def _tx(c: Any) -> int:
            await c.execute(
                """
                INSERT INTO session_versions (slot_id, version, cookie_hash, updated_at)
                VALUES (?, 1, ?, ?)
                ON CONFLICT(slot_id) DO UPDATE SET
                    version = session_versions.version + 1,
                    cookie_hash = CASE WHEN excluded.cookie_hash != '' THEN excluded.cookie_hash
                        ELSE session_versions.cookie_hash END,
                    updated_at = excluded.updated_at
                """,
                (slot_id, cookie_hash, now_ts),
            )
            cur = await c.execute(
                "SELECT version FROM session_versions WHERE slot_id = ?", (slot_id,)
            )
            row = await cur.fetchone()
            return int(row[0]) if row else 1

        return await self.writer.submit(_tx)

    def _validate_admission(self, slot: SlotSnapshot, query_id: str, allow_reserve: bool) -> None:
        if slot.role == SlotRole.RESERVE and not allow_reserve:
            raise ReserveAdmissionError(f"Cannot assign query to reserve slot '{slot.slot_id}'")
        if slot.health in (SlotHealth.DISABLED, SlotHealth.QUARANTINED):
            raise SlotCapacityError(f"Slot '{slot.slot_id}' in state {slot.health.value}")
        if query_id not in slot.capacity.assigned_queries and slot.capacity.is_full:
            raise CapacityExceededError(
                f"Slot '{slot.slot_id}' is full ({slot.capacity.current_load}/4)"
            )

    async def admission_dry_run(
        self, slot_id: str, query_id: str, *, allow_reserve: bool = False
    ) -> SlotSnapshot:
        """Validate capacity without writing an assignment or query row."""
        slot = await self.get_slot(slot_id)
        if slot is None:
            raise SlotCapacityError(f"Slot '{slot_id}' not found")
        self._validate_admission(slot, query_id, allow_reserve)
        return slot

    async def assign_query(
        self, slot_id: str, query_id: str, position: int | None = None, allow_reserve: bool = False
    ) -> int:
        slot = await self.get_slot(slot_id)
        if not slot:
            raise SlotCapacityError(f"Slot '{slot_id}' not found")
        self._validate_admission(slot, query_id, allow_reserve)

        now_ts = datetime.now(UTC).isoformat()

        async def _tx(c: Any) -> int:
            await c.execute(
                "INSERT INTO queries (id, canonical_url) VALUES (?, ?) ON CONFLICT(id) DO NOTHING",
                (query_id, f"https://www.avito.ru/q/{query_id}"),
            )
            await c.execute(
                "UPDATE slots SET active_generation = active_generation + 1, updated_at = ? "
                "WHERE id = ?",
                (now_ts, slot_id),
            )
            cur_g = await c.execute("SELECT active_generation FROM slots WHERE id = ?", (slot_id,))
            new_gen = int((await cur_g.fetchone())[0])

            cur_pos = await c.execute(
                "SELECT position FROM assignments WHERE slot_id = ? AND active = 1 "
                "AND query_id != ?",
                (slot_id, query_id),
            )
            used = {int(r[0]) for r in await cur_pos.fetchall()}
            pos = (
                position
                if position is not None and position not in used
                else next(p for p in range(4) if p not in used)
            )

            asgn_id = f"asgn_{slot_id}_{query_id}_{new_gen}"
            await c.execute(
                """
                INSERT INTO assignments (
                    id, slot_id, query_id, position, generation, active, updated_at
                )
                VALUES (?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT(slot_id, position) DO UPDATE SET
                    id = excluded.id, query_id = excluded.query_id,
                    generation = excluded.generation, active = 1, updated_at = excluded.updated_at
                """,
                (asgn_id, slot_id, query_id, pos, new_gen, now_ts),
            )
            return new_gen

        return await self.writer.submit(_tx)

    async def unassign_query(self, slot_id: str, query_id: str) -> None:
        now_ts = datetime.now(UTC).isoformat()

        async def _tx(c: Any) -> None:
            await c.execute(
                "UPDATE assignments SET active = 0, updated_at = ? "
                "WHERE slot_id = ? AND query_id = ?",
                (now_ts, slot_id, query_id),
            )

        await self.writer.submit(_tx)

    async def transfer_to_spare(
        self, from_slot_id: str, to_spare_slot_id: str, query_id: str
    ) -> int:
        """Transfer an assignment to a ready spare slot atomically."""
        return await transfer_slot_to_spare(self.writer, from_slot_id, to_spare_slot_id, query_id)

    async def record_request_sequence(self, slot_id: str, query_id: str, sequence: int) -> bool:
        """Monotonic CAS barrier: advances high-water mark, rejects late/stale responses."""
        key = self._seq_key(slot_id, query_id)
        seq = int(sequence)

        async def _tx(c: Any) -> bool:
            cur = await c.execute("SELECT value FROM runtime WHERE key = ?", (key,))
            row = await cur.fetchone()
            current_hw = int(row[0]) if row and str(row[0]).isdigit() else 0

            if seq <= current_hw:
                return False

            await c.execute(
                """
                INSERT INTO runtime (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, str(seq)),
            )
            return True

        return await self.writer.submit(_tx)

    async def get_request_sequence(self, slot_id: str, query_id: str) -> int:
        key = self._seq_key(slot_id, query_id)
        async with self._read_conn() as c:
            cur = await c.execute("SELECT value FROM runtime WHERE key = ?", (key,))
            row = await cur.fetchone()
            return int(row[0]) if row and str(row[0]).isdigit() else 0
