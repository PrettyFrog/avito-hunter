from __future__ import annotations

from pathlib import Path

import aiosqlite

from hunter.core.db_parts.schema_path_01 import backup_db_before_migration


async def migrate_v5_to_v6(conn: aiosqlite.Connection, db_path: Path) -> None:
    await backup_db_before_migration(conn, db_path, "v5")
    await conn.execute("BEGIN IMMEDIATE")
    try:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_products (
              chat_id INTEGER PRIMARY KEY,
              step TEXT NOT NULL,
              payload TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              expires_at TEXT NOT NULL
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS feedback (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              avito_id TEXT NOT NULL,
              product_id TEXT,
              feedback TEXT NOT NULL,
              created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_feedback_prod ON feedback(product_id)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_feedback_avito ON feedback(avito_id)"
        )
        await conn.execute("PRAGMA user_version = 6")
        await conn.commit()
    except Exception:
        await conn.execute("ROLLBACK")
        raise


async def _create_v7_users_and_invites_tables(conn: aiosqlite.Connection) -> None:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          telegram_user_id INTEGER UNIQUE NOT NULL,
          chat_id INTEGER NOT NULL,
          username TEXT,
          display_name TEXT,
          role TEXT NOT NULL DEFAULT 'user',
          status TEXT NOT NULL DEFAULT 'active',
          tier TEXT NOT NULL DEFAULT 'friend',
          max_products INTEGER DEFAULT 3,
          min_interval_minutes INTEGER DEFAULT 15,
          max_alerts_per_hour INTEGER DEFAULT 10,
          quiet_start INTEGER,
          quiet_end INTEGER,
          timezone TEXT DEFAULT 'Europe/Moscow',
          invited_by INTEGER REFERENCES users(id),
          created_at TEXT NOT NULL,
          last_seen_at TEXT,
          last_error TEXT
        )
        """
    )
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_users_tg_id ON users(telegram_user_id)")

    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS invites (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          token_hash TEXT UNIQUE NOT NULL,
          label TEXT,
          tier TEXT NOT NULL DEFAULT 'friend',
          created_by INTEGER REFERENCES users(id),
          created_at TEXT NOT NULL,
          expires_at TEXT NOT NULL,
          max_uses INTEGER NOT NULL DEFAULT 1,
          used_count INTEGER NOT NULL DEFAULT 0,
          revoked INTEGER NOT NULL DEFAULT 0
        )
        """
    )


async def _create_v7_requests_and_events_tables(conn: aiosqlite.Connection) -> None:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS access_requests (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          telegram_user_id INTEGER NOT NULL,
          username TEXT,
          display_name TEXT,
          created_at TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending',
          decided_by INTEGER REFERENCES users(id)
        )
        """
    )

    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id INTEGER NOT NULL REFERENCES users(id),
          kind TEXT NOT NULL,
          ts TEXT NOT NULL,
          details TEXT
        )
        """
    )
    await conn.execute(
        'CREATE INDEX IF NOT EXISTS idx_user_events_user_ts ON '
        'user_events(user_id, ts)')


async def create_v7_user_tables(conn: aiosqlite.Connection) -> None:
    await _create_v7_users_and_invites_tables(conn)
    await _create_v7_requests_and_events_tables(conn)

