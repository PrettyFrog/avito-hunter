"""Migration v014: Additive slots, assignments, session_versions, and channel_destinations (S3/S5 / T-466)."""

from __future__ import annotations

import inspect
import logging
from pathlib import Path
import sqlite3
from typing import Any

logger = logging.getLogger(__name__)

V014_SQL = """
CREATE TABLE IF NOT EXISTS slots (
    id TEXT PRIMARY KEY,
    endpoint TEXT NOT NULL,
    account_id TEXT NOT NULL DEFAULT '',
    cookie_namespace TEXT NOT NULL,
    health_state TEXT NOT NULL DEFAULT 'healthy',
    quarantine_until TEXT,
    active_generation INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS assignments (
    id TEXT PRIMARY KEY,
    slot_id TEXT NOT NULL REFERENCES slots(id) ON DELETE CASCADE,
    query_id TEXT NOT NULL REFERENCES queries(id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK(position >= 0 AND position <= 3),
    generation INTEGER NOT NULL DEFAULT 1,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(slot_id, position),
    UNIQUE(slot_id, generation)
);
CREATE TABLE IF NOT EXISTS session_versions (
    slot_id TEXT PRIMARY KEY REFERENCES slots(id) ON DELETE CASCADE,
    version INTEGER NOT NULL DEFAULT 1,
    cookie_hash TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS channel_destinations (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    channel TEXT NOT NULL CHECK(channel IN ('telegram', 'vk')),
    address TEXT NOT NULL,
    verified INTEGER NOT NULL DEFAULT 0 CHECK(verified IN (0, 1)),
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, channel)
);
CREATE INDEX IF NOT EXISTS idx_assignments_slot_active ON assignments(slot_id, active);
CREATE INDEX IF NOT EXISTS idx_assignments_query ON assignments(query_id);
CREATE INDEX IF NOT EXISTS idx_channel_destinations_user ON channel_destinations(user_id);
"""


def _create_backup(db_path: Path) -> None:
    if not db_path.exists() or db_path.stat().st_size == 0 or (bak := db_path.with_name(f"{db_path.name}.bak.v13")).exists():
        return
    src, dst = sqlite3.connect(db_path), sqlite3.connect(bak)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


async def _run(conn: Any, sql: str, script: bool = False) -> None:
    call = conn.executescript(sql) if script else conn.execute(sql)
    if inspect.isawaitable(call):
        await call


async def apply_v014_migration(conn: Any, path: Path | None = None) -> None:
    """Apply migration v14: slots, assignments, session_versions, channel_destinations."""
    if path:
        _create_backup(path)
    await _run(conn, "BEGIN IMMEDIATE")
    try:
        await _run(conn, V014_SQL, script=True)
        await _run(conn, "PRAGMA user_version = 14")
        commit_call = conn.commit()
        if inspect.isawaitable(commit_call):
            await commit_call
    except Exception:
        rollback_call = conn.rollback()
        if inspect.isawaitable(rollback_call):
            await rollback_call
        raise


