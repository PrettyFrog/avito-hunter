"""Migration v016: Additive query_groups, group_members, and price_watch_jobs (S7 / T-503)."""

from __future__ import annotations

import inspect
import logging
from pathlib import Path
import sqlite3
from typing import Any

logger = logging.getLogger(__name__)

V016_SQL = """
CREATE TABLE IF NOT EXISTS query_groups (
    id TEXT PRIMARY KEY,
    canonical_url TEXT NOT NULL,
    filter_fingerprint TEXT NOT NULL,
    max_price INTEGER NOT NULL,
    generation INTEGER NOT NULL DEFAULT 1,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS group_members (
    id TEXT PRIMARY KEY,
    group_id TEXT NOT NULL REFERENCES query_groups(id) ON DELETE CASCADE,
    subscription_id TEXT NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    personal_max_price INTEGER NOT NULL,
    stopwords TEXT NOT NULL DEFAULT '[]',
    generation INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(group_id, subscription_id)
);
CREATE TABLE IF NOT EXISTS price_watch_jobs (
    id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL,
    query_group_id TEXT REFERENCES query_groups(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    lease_token TEXT,
    lease_until TEXT,
    next_run_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_group_members_group ON group_members(group_id);
CREATE INDEX IF NOT EXISTS idx_group_members_sub ON group_members(subscription_id);
CREATE INDEX IF NOT EXISTS idx_group_members_user ON group_members(user_id);
CREATE INDEX IF NOT EXISTS idx_price_watch_jobs_due ON price_watch_jobs(status, next_run_at);
CREATE INDEX IF NOT EXISTS idx_price_watch_jobs_lease ON price_watch_jobs(lease_token) WHERE lease_token IS NOT NULL;
"""


def _create_backup(db_path: Path) -> None:
    if not db_path.exists() or db_path.stat().st_size == 0 or (bak := db_path.with_name(f"{db_path.name}.bak.v15")).exists():
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


async def apply_v016_migration(conn: Any, path: Path | None = None) -> None:
    """Apply migration v16: query_groups, group_members, and price_watch_jobs."""
    if path:
        _create_backup(path)
    await _run(conn, "BEGIN IMMEDIATE")
    try:
        await _run(conn, V016_SQL, script=True)
        await _run(conn, "PRAGMA user_version = 16")
        commit_call = conn.commit()
        if inspect.isawaitable(commit_call):
            await commit_call
    except Exception:
        rollback_call = conn.rollback()
        if inspect.isawaitable(rollback_call):
            await rollback_call
        raise



