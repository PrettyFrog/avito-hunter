"""Persist single-use channel binding nonces across restarts."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any


V017_SQL = """
CREATE TABLE IF NOT EXISTS binding_consumed_nonces (
    nonce TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    channel TEXT NOT NULL,
    consumed_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


async def _run(conn: Any, sql: str) -> None:
    result = conn.execute(sql)
    if inspect.isawaitable(result):
        await result


async def apply_v017_migration(conn: Any, path: Path | None = None) -> None:
    """Add the durable nonce fence for VK and other destination bindings."""
    await _run(conn, "BEGIN IMMEDIATE")
    try:
        await _run(conn, V017_SQL)
        await _run(conn, "PRAGMA user_version = 17")
        committed = conn.commit()
        if inspect.isawaitable(committed):
            await committed
    except Exception:
        rolled_back = conn.rollback()
        if inspect.isawaitable(rolled_back):
            await rolled_back
        raise
