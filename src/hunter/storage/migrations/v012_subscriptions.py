"""Migration v012: Additive queries, subscriptions, query_membership, subscription_seen (S3 / T-459)."""

from __future__ import annotations

import logging
from pathlib import Path
import sqlite3
from typing import Any

logger = logging.getLogger(__name__)

V012_SQL = """
CREATE TABLE IF NOT EXISTS queries (
    id TEXT PRIMARY KEY,
    canonical_url TEXT UNIQUE NOT NULL,
    contract_version INTEGER NOT NULL DEFAULT 1,
    bootstrap_state TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    query_id TEXT NOT NULL REFERENCES queries(id) ON DELETE CASCADE,
    max_price INTEGER NOT NULL,
    manual_stopwords TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    version INTEGER NOT NULL DEFAULT 1,
    ai_enrichment_enabled INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(user_id, query_id)
);

CREATE TABLE IF NOT EXISTS query_membership (
    query_id TEXT NOT NULL REFERENCES queries(id) ON DELETE CASCADE,
    avito_id TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    baseline_epoch INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (query_id, avito_id)
);

CREATE TABLE IF NOT EXISTS subscription_seen (
    subscription_id TEXT NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
    avito_id TEXT NOT NULL,
    baseline_epoch INTEGER NOT NULL DEFAULT 0,
    last_revision INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (subscription_id, avito_id)
);

CREATE INDEX IF NOT EXISTS idx_subscriptions_user ON subscriptions(user_id);
CREATE INDEX IF NOT EXISTS idx_subscriptions_query ON subscriptions(query_id);
CREATE INDEX IF NOT EXISTS idx_query_membership_avito ON query_membership(avito_id);
CREATE INDEX IF NOT EXISTS idx_subscription_seen_avito ON subscription_seen(avito_id);
"""


def _create_backup(db_path: Path) -> None:
    if not db_path.exists() or db_path.stat().st_size == 0:
        return
    backup_path = db_path.with_name(f"{db_path.name}.bak.v11")
    if backup_path.exists():
        return
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(backup_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


async def apply_v012_migration(conn: Any, path: Path | None = None) -> None:
    """Apply migration v12: create subscriptions and membership tables."""
    if path:
        _create_backup(path)

    await conn.execute("BEGIN IMMEDIATE")
    try:
        await conn.executescript(V012_SQL)
        await conn.execute("PRAGMA user_version = 12")
        await conn.commit()
        logger.info("Database migration to version 12 (subscriptions & memberships) completed successfully")
    except Exception:
        await conn.execute("ROLLBACK")
        raise
