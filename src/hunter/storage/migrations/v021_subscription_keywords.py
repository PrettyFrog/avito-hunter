"""Add manual_keywords to subscriptions table."""
from __future__ import annotations
import inspect
from typing import Any

V021_CHECKSUM_INPUT = "subscriptions.manual_keywords text not null default empty"


async def _run(conn: Any, sql: str) -> Any:
    res = conn.execute(sql)
    return await res if inspect.isawaitable(res) else res


async def apply_v021_migration(conn: Any, path: Any = None) -> None:
    await _run(conn, "BEGIN IMMEDIATE")
    try:
        cur = await _run(conn, "PRAGMA table_info(subscriptions)")
        rows = await cur if inspect.isawaitable(cur) else cur.fetchall()
        if inspect.isawaitable(rows):
            rows = await rows
        if rows and "manual_keywords" not in {str(r[1]) for r in rows}:
            await _run(conn, "ALTER TABLE subscriptions ADD COLUMN manual_keywords TEXT NOT NULL DEFAULT ''")
        await _run(conn, "PRAGMA user_version = 21")
        res = conn.commit()
        if inspect.isawaitable(res):
            await res
    except BaseException:
        res = conn.rollback()
        if inspect.isawaitable(res):
            await res
        raise
