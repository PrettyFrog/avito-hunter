"""Persist explicit slot role instead of deriving reserve status from slot IDs."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any


V018_ADD_ROLE_SQL = (
    "ALTER TABLE slots ADD COLUMN role TEXT NOT NULL DEFAULT 'active' "
    "CHECK(role IN ('active', 'reserve'))"
)
V018_BACKFILL_SQL = "UPDATE slots SET role = 'reserve' WHERE lower(id) LIKE '%res%'"
# Freeze checksum input on first local application of v18; applied DBs must remain readable.
V018_CHECKSUM_INPUT = "slots.role=active|reserve; legacy reserve ID backfill"


async def _run(conn: Any, sql: str) -> None:
    result = conn.execute(sql)
    if inspect.isawaitable(result):
        await result


async def apply_v018_migration(conn: Any, path: Path | None = None) -> None:
    """Add role to existing slots and preserve the legacy ID-based classification once."""
    await _run(conn, "BEGIN IMMEDIATE")
    try:
        cursor = conn.execute("PRAGMA table_info(slots)")
        if inspect.isawaitable(cursor):
            cursor = await cursor
        rows = cursor.fetchall()
        if inspect.isawaitable(rows):
            rows = await rows
        if "role" not in {str(row[1]) for row in rows}:
            await _run(conn, V018_ADD_ROLE_SQL)
        await _run(conn, V018_BACKFILL_SQL)
        await _run(conn, "PRAGMA user_version = 18")
        result = conn.commit()
        if inspect.isawaitable(result):
            await result
    except Exception:
        result = conn.rollback()
        if inspect.isawaitable(result):
            await result
        raise
