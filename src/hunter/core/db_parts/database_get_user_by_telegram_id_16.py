"""Database user lookup and lifecycle helpers (T-190)."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import aiosqlite
from loguru import logger

from hunter.core.models import now_utc


async def get_user_by_telegram_id_db(db: Any, telegram_user_id: int) -> dict | None:
    conn = await db.connect()
    cur = await conn.execute("SELECT * FROM users WHERE telegram_user_id = ?", (telegram_user_id,))
    row = await cur.fetchone()
    return dict(row) if row else None


async def get_user_by_id_db(db: Any, user_id: int) -> dict | None:
    conn = await db.connect()
    cur = await conn.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    row = await cur.fetchone()
    return dict(row) if row else None


async def get_owner_user_db(db: Any) -> dict | None:
    conn = await db.connect()
    cur = await conn.execute("SELECT * FROM users WHERE role = 'owner' ORDER BY id ASC LIMIT 1")
    row = await cur.fetchone()
    return dict(row) if row else None


async def set_user_status_db(
    db: Any, user_id: int, status: str, last_error: str | None = None
) -> None:
    conn = await db.connect()
    now_str = now_utc().isoformat()
    if last_error is not None:
        await conn.execute(
            "UPDATE users SET status = ?, last_error = ?, last_seen_at = ? WHERE id = ?",
            (status, last_error, now_str, user_id),
        )
    else:
        await conn.execute(
            "UPDATE users SET status = ?, last_seen_at = ? WHERE id = ?",
            (status, now_str, user_id),
        )
    await conn.commit()


async def update_user_last_seen_db(
    db: Any,
    user_id: int,
    username: str | None = None,
    display_name: str | None = None,
) -> None:
    try:
        conn = await db.connect()
        now_str = now_utc().isoformat()
        if username or display_name:
            await conn.execute(
                """
                UPDATE users
                SET last_seen_at = ?,
                    username = COALESCE(?, username),
                    display_name = COALESCE(?, display_name)
                WHERE id = ?
                """,
                (now_str, username, display_name, user_id),
            )
        else:
            await conn.execute("UPDATE users SET last_seen_at = ? WHERE id = ?", (now_str, user_id))
        await conn.commit()
    except (sqlite3.OperationalError, Exception) as exc:
        logger.debug("Failed updating user last_seen (non-critical): {}", exc)


async def log_user_event_db(db: Any, user_id: int, kind: str, details: Any = None) -> None:
    conn = await db.connect()
    now_str = now_utc().isoformat()
    det_str = (
        json.dumps(details, ensure_ascii=False)
        if isinstance(details, (dict, list))
        else (str(details) if details else None)
    )
    await conn.execute(
        "INSERT INTO user_events (user_id, kind, ts, details) VALUES (?, ?, ?, ?)",
        (user_id, kind, now_str, det_str),
    )
    await conn.commit()


async def get_active_users_db(db: Any) -> list[dict]:
    conn = await db.connect()
    cur = await conn.execute("SELECT * FROM users WHERE status = 'active' ORDER BY id ASC")
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_all_users_db(db: Any) -> list[dict]:
    conn = await db.connect()
    cur = await conn.execute("SELECT * FROM users ORDER BY id ASC")
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def find_user_by_identifier_db(db: Any, ident: str | int) -> dict | None:
    """Find user by internal id, telegram_user_id, or username (with or without @)."""
    conn = await db.connect()
    if isinstance(ident, int) or (isinstance(ident, str) and ident.isdigit()):
        uid = int(ident)
        cur = await conn.execute(
            'SELECT * FROM users WHERE id = ? OR telegram_user_id = ? LIMIT '
            '1', (uid, uid))
        row = await cur.fetchone()
        if row:
            return dict(row)
    clean_user = str(ident).lstrip("@").strip().lower()
    cur = await conn.execute("SELECT * FROM users WHERE LOWER(username) = ? LIMIT 1", (clean_user,))
    row = await cur.fetchone()
    return dict(row) if row else None


async def _delete_user_related_records(
    conn: aiosqlite.Connection,
    user_id: int,
    chat_id: Any,
    tg_id: Any,
) -> dict[str, int]:
    stats: dict[str, int] = {}
    cur = await conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {r[0] for r in await cur.fetchall()}

    for tbl, sql, params in [
        ("support_tickets", "DELETE FROM support_tickets WHERE user_id = ? OR (chat_id = ? AND ? IS NOT NULL)", (user_id, chat_id, chat_id)),
        ("notifications", "DELETE FROM notifications WHERE owner_user_id = ? OR recipient_user_id = ?", (user_id, user_id)),
        ("digest_queue", "DELETE FROM digest_queue WHERE user_id = ?", (user_id,)),
        ("pending_products", "DELETE FROM pending_products WHERE owner_user_id = ? OR (chat_id = ? AND ? IS NOT NULL)", (user_id, chat_id, chat_id)),
        ("feedback", "DELETE FROM feedback WHERE owner_user_id = ?", (user_id,)),
        ("user_events", "DELETE FROM user_events WHERE user_id = ?", (user_id,)),
        ("access_requests", "DELETE FROM access_requests WHERE (telegram_user_id = ? AND ? IS NOT NULL) OR decided_by = ?", (tg_id, tg_id, user_id)),
        ("invites", "DELETE FROM invites WHERE created_by = ?", (user_id,)),
    ]:
        if tbl in tables:
            cur = await conn.execute(sql, params)
            stats[tbl] = cur.rowcount
        else:
            stats[tbl] = 0

    if "users" in tables:
        await conn.execute("UPDATE users SET invited_by = NULL WHERE invited_by = ?", (user_id,))
    return stats


async def _anonymize_user_record(conn: aiosqlite.Connection, user_id: int) -> None:
    await conn.execute(
        """
        UPDATE users
        SET telegram_user_id = NULL,
            chat_id = NULL,
            username = NULL,
            display_name = NULL,
            status = 'deleted',
            quiet_start = NULL,
            quiet_end = NULL,
            last_error = NULL,
            invited_by = NULL
        WHERE id = ?
        """,
        (user_id,),
    )


async def delete_user_data_db(db: Any, user_id: int) -> dict[str, int]:
    """Purges all user data across all tables and nullifies identifiers in users table.
    Leaves anonymized counters. Owner cannot be deleted.
    """
    from hunter.storage.user_deletion import delete_public_user

    writer = getattr(db, "_public_writer", None)
    if writer is not None:
        return await writer.submit(lambda conn: delete_public_user(conn, user_id))
    user = await db.get_user_by_id(user_id)
    if not user:
        return {}
    if user.get("role") == "owner":
        raise ValueError("Owner cannot be deleted, blocked, or paused")

    conn = await db.connect()
    stats = await delete_public_user(conn, user_id)
    await conn.commit()
    return stats
