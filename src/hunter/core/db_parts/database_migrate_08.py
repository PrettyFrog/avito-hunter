"""Database migration helper slice 08 (T-182, T-183)."""

from pathlib import Path

import aiosqlite
from loguru import logger

from hunter.core.config import get_settings
from hunter.core.db_parts.schema_path_01 import backup_db_before_migration
from hunter.core.models import now_utc


async def _add_v7_columns(conn: aiosqlite.Connection) -> None:
    notif_cols = [
        r[1] for r in await (await conn.execute("PRAGMA table_info(notifications)")).fetchall()
    ]
    if notif_cols:
        if "owner_user_id" not in notif_cols:
            await conn.execute(
                "ALTER TABLE notifications ADD COLUMN owner_user_id INTEGER REFERENCES users(id)"
            )
        if "recipient_user_id" not in notif_cols:
            await conn.execute(
                "ALTER TABLE notifications ADD COLUMN recipient_user_id INTEGER REFERENCES users(id)"
            )
        if "photo_key" not in notif_cols:
            await conn.execute("ALTER TABLE notifications ADD COLUMN photo_key TEXT")

    fb_cols = [r[1] for r in await (await conn.execute("PRAGMA table_info(feedback)")).fetchall()]
    if fb_cols and "owner_user_id" not in fb_cols:
        await conn.execute(
            "ALTER TABLE feedback ADD COLUMN owner_user_id INTEGER REFERENCES users(id)"
        )

    pending_cols = [
        r[1] for r in await (await conn.execute("PRAGMA table_info(pending_products)")).fetchall()
    ]
    if pending_cols and "owner_user_id" not in pending_cols:
        await conn.execute(
            "ALTER TABLE pending_products ADD COLUMN owner_user_id INTEGER REFERENCES users(id)"
        )

    prod_cols = [r[1] for r in await (await conn.execute("PRAGMA table_info(products)")).fetchall()]
    if prod_cols and "owner_user_id" not in prod_cols:
        await conn.execute(
            "ALTER TABLE products ADD COLUMN owner_user_id INTEGER REFERENCES users(id)"
        )


async def _seed_v7_owner(conn: aiosqlite.Connection) -> int | None:
    settings = get_settings()
    raw_owner_id = getattr(settings, "telegram_chat_id", None)
    if not raw_owner_id:
        return None
    try:
        owner_tg_id = int(raw_owner_id)
        now_str = now_utc().isoformat()
        cur = await conn.execute("SELECT id FROM users WHERE role = 'owner' LIMIT 1")
        owner_row = await cur.fetchone()
        if owner_row:
            return owner_row[0]
        cur = await conn.execute(
            "\n            INSERT INTO users (telegram_user_id, chat_id, role, status, tier, "
            "display_name, created_at)\n            VALUES (?, ?, 'owner', 'active', 'owner', "
            "'Owner', ?)\n            ",
            (owner_tg_id, owner_tg_id, now_str),
        )
        return cur.lastrowid
    except (ValueError, TypeError):
        return None


async def _backfill_v7_owner_user_id(conn: aiosqlite.Connection, owner_db_id: int | None) -> None:
    if not owner_db_id:
        return
    cur = await conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {r[0] for r in await cur.fetchall()}
    if "notifications" in tables:
        await conn.execute(
            "UPDATE notifications SET owner_user_id = ?, recipient_user_id = ? "
            "WHERE owner_user_id IS NULL",
            (owner_db_id, owner_db_id),
        )
    if "feedback" in tables:
        await conn.execute(
            "UPDATE feedback SET owner_user_id = ? WHERE owner_user_id IS NULL",
            (owner_db_id,),
        )
    if "pending_products" in tables:
        await conn.execute(
            "UPDATE pending_products SET owner_user_id = ? WHERE owner_user_id IS NULL",
            (owner_db_id,),
        )


async def apply_v7_user_migration(conn: aiosqlite.Connection) -> None:
    await _add_v7_columns(conn)
    owner_db_id = await _seed_v7_owner(conn)
    await _backfill_v7_owner_user_id(conn, owner_db_id)


async def migrate_v7_to_v8(conn: aiosqlite.Connection, db_path: Path) -> None:
    await backup_db_before_migration(conn, db_path, "v7")
    await conn.execute("BEGIN IMMEDIATE")
    try:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS digest_queue (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id INTEGER NOT NULL REFERENCES users(id),
              avito_id TEXT NOT NULL,
              payload TEXT NOT NULL,
              reason TEXT NOT NULL,
              created_at TEXT NOT NULL
            )
            """
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_digest_queue_user ON digest_queue(user_id, created_at)"
        )
        cur = await conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {r[0] for r in await cur.fetchall()}
        if "notifications" in tables:
            cols = [r[1] for r in await (await conn.execute("PRAGMA table_info(notifications)")).fetchall()]
            if "recipient_user_id" in cols and "avito_id" in cols:
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_notif_user_avito "
                    "ON notifications(recipient_user_id, avito_id)"
                )
            if "recipient_user_id" in cols and "sent_at" in cols:
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_notif_user_sent "
                    "ON notifications(recipient_user_id, sent_at)"
                )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_events_rate ON user_events(user_id, kind, ts)"
        )

        await conn.execute("PRAGMA user_version = 8")
        await conn.commit()
        logger.info("Database migration to version 8 (Multi-User M4) completed successfully")
    except Exception:
        await conn.execute("ROLLBACK")
        raise
