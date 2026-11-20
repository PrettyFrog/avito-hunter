"""Database migration helper slice 10 (T-184, T-185)."""

from pathlib import Path
from typing import Any

import aiosqlite
from loguru import logger

from hunter.core.db_parts.schema_path_01 import backup_db_before_migration


async def _rebuild_users_table(conn: aiosqlite.Connection, user_cols: list[Any]) -> None:
    await conn.commit()
    await conn.execute("PRAGMA foreign_keys = OFF")
    try:
        await conn.execute(
            """
            CREATE TABLE users_new (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              telegram_user_id INTEGER UNIQUE,
              chat_id INTEGER,
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
        existing_col_names = {c[1] for c in user_cols}
        new_cols = [
            "id", "telegram_user_id", "chat_id", "username", "display_name", "role",
            "status", "tier", "max_products", "min_interval_minutes", "max_alerts_per_hour",
            "quiet_start", "quiet_end", "timezone", "invited_by", "created_at", "last_seen_at", "last_error",
        ]
        cols_to_copy = [c for c in new_cols if c in existing_col_names]
        cols_str = ", ".join(cols_to_copy)
        await conn.execute(f"INSERT INTO users_new ({cols_str}) SELECT {cols_str} FROM users")
        await conn.execute("DROP TABLE users")
        await conn.execute("ALTER TABLE users_new RENAME TO users")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_users_tg_id ON users(telegram_user_id)")
        await conn.commit()
    finally:
        await conn.execute("PRAGMA foreign_keys = ON")


async def migrate_v8_users_table_if_needed(conn: aiosqlite.Connection) -> None:
    user_cols = await (await conn.execute("PRAGMA table_info(users)")).fetchall()
    tg_col = next((c for c in user_cols if c[1] == "telegram_user_id"), None)
    if bool(tg_col and tg_col[3] == 1):
        await _rebuild_users_table(conn, user_cols)


async def _create_v9_tables(conn: aiosqlite.Connection) -> None:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS support_tickets (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id INTEGER NOT NULL REFERENCES users(id),
          chat_id INTEGER NOT NULL,
          text TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'open',
          created_at TEXT NOT NULL,
          answered_at TEXT,
          answered_by INTEGER REFERENCES users(id),
          response_text TEXT,
          error TEXT
        )
        """
    )
    await conn.execute(
        'CREATE INDEX IF NOT EXISTS '
        'idx_support_tickets_user ON '
        'support_tickets(user_id, created_at)'
    )
    await conn.execute(
        'CREATE INDEX IF NOT EXISTS '
        'idx_support_tickets_status ON '
        'support_tickets(status, created_at)'
    )

    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS broadcasts (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          sender_user_id INTEGER REFERENCES users(id),
          target_audience TEXT NOT NULL,
          text TEXT NOT NULL,
          delivered_count INTEGER NOT NULL DEFAULT 0,
          failed_count INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL
        )
        """
    )
    await conn.execute(
        'CREATE INDEX IF NOT EXISTS idx_broadcasts_created '
        'ON broadcasts(created_at)'
    )


async def migrate_v8_to_v9(conn: aiosqlite.Connection, db_path: Path) -> None:
    await backup_db_before_migration(conn, db_path, "v8")
    await migrate_v8_users_table_if_needed(conn)
    await conn.execute("BEGIN IMMEDIATE")
    try:
        await _create_v9_tables(conn)
        await conn.execute("PRAGMA user_version = 9")
        await conn.commit()
        logger.info("Database migration to version 9 (Multi-User M5) completed successfully")
    except Exception:
        await conn.execute("ROLLBACK")
        raise


async def migrate_v9_to_v10(conn: aiosqlite.Connection, db_path: Path) -> None:
    await backup_db_before_migration(conn, db_path, "v9")
    await conn.execute("BEGIN IMMEDIATE")
    try:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS photo_checks (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              avito_id TEXT NOT NULL,
              product_id TEXT NOT NULL,
              method TEXT NOT NULL,
              photo_hash TEXT NOT NULL,
              signature_version TEXT NOT NULL,
              prompt_version TEXT NOT NULL,
              provider TEXT NOT NULL,
              model_version TEXT NOT NULL,
              result_json TEXT NOT NULL,
              created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        await conn.execute(
                            'CREATE INDEX IF NOT EXISTS idx_photo_checks_lookup'
                ' ON photo_checks(photo_hash, signature_version, '
                'prompt_version, method, provider)'
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_photo_checks_avito ON photo_checks(avito_id)"
        )
        await conn.execute("PRAGMA user_version = 10")
        await conn.commit()
        logger.info("Database migration to version 10 (PH-1 photo_checks) completed successfully")
    except Exception:
        await conn.execute("ROLLBACK")
        raise


async def migrate_v10_to_v11(conn: aiosqlite.Connection, db_path: Path) -> None:
    await backup_db_before_migration(conn, db_path, "v10")
    await conn.execute("BEGIN IMMEDIATE")
    try:
        listing_cols = [
            r[1] for r in await (await conn.execute("PRAGMA table_info(listings)")).fetchall()
        ]
        if "published_at" not in listing_cols:
            await conn.execute("ALTER TABLE listings ADD COLUMN published_at TEXT")
        await conn.execute("PRAGMA user_version = 11")
        await conn.commit()
        logger.info(
            'Database migration to version 11 (published_at in '
            'listings) completed successfully'
        )
    except Exception:
        await conn.execute("ROLLBACK")
        raise


