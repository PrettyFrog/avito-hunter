"""Migration v013: Additive listing_state, events, recipient_events, deliveries (S3 / T-460)."""

from __future__ import annotations

import inspect
import logging
from pathlib import Path
import sqlite3
from typing import Any

logger = logging.getLogger(__name__)

V013_SQL = """
CREATE TABLE IF NOT EXISTS listing_state (
    avito_id TEXT PRIMARY KEY,
    known_price INTEGER,
    price_state TEXT NOT NULL DEFAULT 'active',
    revision INTEGER NOT NULL DEFAULT 1,
    last_request_sequence INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    avito_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('NEW_LISTING', 'PRICE_CHANGED')),
    observed_at TEXT NOT NULL,
    old_price INTEGER,
    new_price INTEGER,
    origin TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(avito_id, revision, kind)
);

CREATE TABLE IF NOT EXISTS recipient_events (
    user_id INTEGER NOT NULL,
    event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, event_id)
);

CREATE TABLE IF NOT EXISTS deliveries (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL,
    channel TEXT NOT NULL,
    destination_version INTEGER NOT NULL DEFAULT 1,
    payload_version INTEGER NOT NULL DEFAULT 1,
    payload TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    available_at TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    lease_token TEXT,
    lease_until TEXT,
    message_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(user_id, event_id, channel)
);

CREATE INDEX IF NOT EXISTS idx_events_avito ON events(avito_id, revision);
CREATE INDEX IF NOT EXISTS idx_recipient_events_user ON recipient_events(user_id);
CREATE INDEX IF NOT EXISTS idx_deliveries_due ON deliveries(status, available_at);
CREATE INDEX IF NOT EXISTS idx_deliveries_user_event ON deliveries(user_id, event_id);
"""


def _create_backup(db_path: Path) -> None:
    if not db_path.exists() or db_path.stat().st_size == 0:
        return
    backup_path = db_path.with_name(f"{db_path.name}.bak.v12")
    if backup_path.exists():
        return
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(backup_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


async def _run(conn: Any, sql: str, script: bool = False) -> None:
    call = conn.executescript(sql) if script else conn.execute(sql)
    if inspect.isawaitable(call):
        await call


async def apply_v013_migration(conn: Any, path: Path | None = None) -> None:
    """Apply migration v13: create listing state, events, and delivery outbox tables."""
    if path:
        _create_backup(path)

    await _run(conn, "BEGIN IMMEDIATE")
    try:
        await _run(conn, V013_SQL, script=True)
        await _run(conn, "PRAGMA user_version = 13")
        commit_call = conn.commit()
        if inspect.isawaitable(commit_call):
            await commit_call
        logger.info("Database migration to version 13 (events & deliveries) completed successfully")
    except Exception:
        rollback_call = conn.execute("ROLLBACK")
        if inspect.isawaitable(rollback_call):
            await rollback_call
        raise
