"""Add a per-subscription display name without changing shared query identity."""
from __future__ import annotations

import inspect
from typing import Any

V020_CHECKSUM_INPUT = "subscriptions.name text not null default empty"


async def _run(conn: Any, sql: str) -> Any:
    result = conn.execute(sql)
    return await result if inspect.isawaitable(result) else result


async def apply_v020_migration(conn: Any, path: Any = None) -> None:
    await _run(conn, "BEGIN IMMEDIATE")
    try:
        cursor = await _run(conn, "PRAGMA table_info(subscriptions)")
        rows = cursor.fetchall()
        if inspect.isawaitable(rows):
            rows = await rows
        if rows and "name" not in {str(row[1]) for row in rows}:
            await _run(conn, "ALTER TABLE subscriptions ADD COLUMN name TEXT NOT NULL DEFAULT ''")
        await _run(conn, "PRAGMA user_version = 20")
        result = conn.commit()
        if inspect.isawaitable(result):
            await result
    except BaseException:
        result = conn.rollback()
        if inspect.isawaitable(result):
            await result
        raise
