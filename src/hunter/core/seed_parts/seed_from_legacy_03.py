"""Seed slice 03: Blocklist transfer and integrity verification (T-232)."""

from __future__ import annotations

from typing import Any

import aiosqlite


async def transfer_blocklist(
    new_conn: aiosqlite.Connection,
    old_conn: aiosqlite.Connection,
    old_blocklist: list[Any],
) -> None:
    """Transfer blocklist table records if present."""
    if not old_blocklist:
        return
    cur = await new_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND "
        "name='blocklist'")
    if await cur.fetchone():
        cur = await new_conn.execute("PRAGMA table_info(blocklist)")
        new_bl_cols = [r[1] for r in await cur.fetchall()]
        cur = await old_conn.execute("PRAGMA table_info(blocklist)")
        old_bl_cols = {r[1] for r in await cur.fetchall()}
        common_bl_cols = [c for c in new_bl_cols if c in old_bl_cols]
        ph_bl = ", ".join("?" for _ in common_bl_cols)
        sql_insert_bl = f"INSERT OR IGNORE INTO blocklist ({
            ', '
            ''.join(common_bl_cols)}) VALUES ({ph_bl})"
        await new_conn.executemany(
            sql_insert_bl, [[dict(r).get(c) for c in common_bl_cols] for r in old_blocklist]
        )


async def verify_new_db_integrity(new_conn: aiosqlite.Connection) -> None:
    """Verify foreign key constraints and database integrity."""
    cur = await new_conn.execute("PRAGMA foreign_key_check")
    fk_errors = await cur.fetchall()
    if fk_errors:
        raise RuntimeError(f"Foreign key violations after seeding: {fk_errors}")

    cur = await new_conn.execute("PRAGMA integrity_check")
    rows = await cur.fetchall()
    integrity = [r[0] for r in rows]
    if integrity != ["ok"]:
        raise RuntimeError(f"Integrity check failed after seeding: {integrity}")
