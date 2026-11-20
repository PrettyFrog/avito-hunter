"""Database slice 25: Digest queue popping and user limits operations."""

from collections.abc import Awaitable, Callable
from typing import Any
import aiosqlite


async def get_digest_items_for_user_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    user_id: int,
    reason: str | None = None,
) -> list[dict[str, Any]]:
    """Retrieves pending digest items for a user without deleting them."""
    conn = await connect()
    if reason:
        cur = await conn.execute(
            'SELECT id, user_id, avito_id, payload, reason, created_at FROM '
                'digest_queue WHERE user_id = ? AND reason = ? ORDER BY id ASC',
            (user_id, reason),
        )
    else:
        cur = await conn.execute(
            'SELECT id, user_id, avito_id, payload, reason, created_at FROM '
                'digest_queue WHERE user_id = ? ORDER BY id ASC',
            (user_id,),
        )
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def delete_digest_items_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    item_ids: list[int],
) -> None:
    """Deletes specific confirmed digest items by IDs."""
    if not item_ids:
        return
    conn = await connect()
    placeholders = ",".join("?" for _ in item_ids)
    await conn.execute(f"DELETE FROM digest_queue WHERE id IN ({placeholders})", item_ids)
    await conn.commit()


async def pop_digest_items_for_user_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    user_id: int,
    reason: str | None = None,
) -> list[dict[str, Any]]:
    """Atomically retrieves and clears digest items for a user."""
    items = await get_digest_items_for_user_db(connect, user_id, reason)
    if items:
        await delete_digest_items_db(connect, [it["id"] for it in items])
    return items



async def get_users_with_pending_digests_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    reason: str | None = None,
) -> list[int]:
    """Returns list of distinct user_ids having pending items in digest_queue."""
    conn = await connect()
    if reason:
        cur = await conn.execute(
            'SELECT DISTINCT user_id FROM digest_queue WHERE reason = '
            '?', (reason,))
    else:
        cur = await conn.execute("SELECT DISTINCT user_id FROM digest_queue")
    rows = await cur.fetchall()
    return [int(r[0]) for r in rows]


async def update_user_limits_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    user_id: int,
    max_products: int | None = None,
    min_interval_minutes: int | None = None,
) -> None:
    """Updates max_products and/or min_interval_minutes for a user."""
    conn = await connect()
    updates = []
    params = []
    if max_products is not None:
        updates.append("max_products = ?")
        params.append(max_products)
    if min_interval_minutes is not None:
        updates.append("min_interval_minutes = ?")
        params.append(min_interval_minutes)
    if updates:
        params.append(user_id)
        await conn.execute(f"UPDATE users SET {', '.join(updates)} WHERE id = ?", params)
        await conn.commit()


async def update_user_quiet_hours_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    user_id: int,
    start_hour: int | None,
    end_hour: int | None,
) -> None:
    """Sets quiet hours (e.g. 23 to 8) or disables them if None."""
    conn = await connect()
    await conn.execute(
        "UPDATE users SET quiet_start = ?, quiet_end = ? WHERE id = ?",
        (start_hour, end_hour, user_id),
    )
    await conn.commit()


async def get_active_user_chat_ids_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    tier: str | None = None,
) -> list[int]:
    """Returns chat IDs of all active users, optionally filtered by tier."""
    conn = await connect()
    if tier:
        cur = await conn.execute(
            "SELECT DISTINCT chat_id FROM users WHERE status = 'active' AND chat_id "
                'IS NOT NULL AND chat_id != 0 AND tier = ?',
            (tier,),
        )
    else:
        cur = await conn.execute(
            "SELECT DISTINCT chat_id FROM users WHERE status = 'active' AND chat_id "
                'IS NOT NULL AND chat_id != 0'
        )
    rows = await cur.fetchall()
    return [int(r[0]) for r in rows]


async def get_last_user_notification_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    user_id: int,
    avito_id: str,
) -> dict[str, Any] | None:
    """Returns the most recent notification for (user_id, avito_id)."""
    conn = await connect()
    cur = await conn.execute(
        """
        SELECT * FROM notifications 
        WHERE recipient_user_id = ? AND avito_id = ? 
        ORDER BY id DESC LIMIT 1
        """,
        (user_id, avito_id),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def find_recent_notification_by_image_key_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    image_key: str,
    *,
    recipient_user_id: int | None = None,
    days: int = 14,
) -> dict[str, Any] | None:
    """Returns the most recent sent notification matching image_key within N days."""
    if not image_key:
        return None
    conn = await connect()
    time_limit = f"-{int(days * 24)} hours"
    if recipient_user_id:
        cur = await conn.execute(
            """
            SELECT * FROM notifications 
            WHERE photo_key = ? 
              AND (recipient_user_id = ? OR recipient_user_id IS NULL)
              AND status = 'sent'
              AND datetime(sent_at) >= datetime('now', ?)
            ORDER BY id DESC LIMIT 1
            """,
            (image_key, recipient_user_id, time_limit),
        )
    else:
        cur = await conn.execute(
            """
            SELECT * FROM notifications 
            WHERE photo_key = ? 
              AND status = 'sent'
              AND datetime(sent_at) >= datetime('now', ?)
            ORDER BY id DESC LIMIT 1
            """,
            (image_key, time_limit),
        )
    row = await cur.fetchone()
    return dict(row) if row else None


async def get_recent_notification_by_fingerprint_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    fingerprint: str,
    *,
    current_avito_id: str | None = None,
    watch_id: str | None = None,
    recipient_user_id: int | None = None,
    within_hours: int = 48,
) -> dict[str, Any] | None:
    (
        'Returns the most recent sent notification for an item with the same '
        'fingerprint within N hours.'
    )
    if not fingerprint:
        return None
    conn = await connect()
    time_limit = f"-{int(within_hours)} hours"
    cur_avito = current_avito_id or ""
    if recipient_user_id:
        where_extra = "AND n.recipient_user_id = ?"
        param_extra: tuple[Any, ...] = (recipient_user_id,)
    elif watch_id:
        where_extra = "AND n.idempotency_key LIKE ?"
        param_extra = (f"%:{watch_id}",)
    else:
        where_extra = ""
        param_extra = ()

    sql = f"""
        SELECT n.*, l.price AS listing_price FROM notifications n
        JOIN listings l ON n.avito_id = l.avito_id
        WHERE l.fingerprint = ?
          AND n.avito_id != ?
          {where_extra}
          AND n.status = 'sent'
          AND datetime(n.sent_at) >= datetime('now', ?)
        ORDER BY n.id DESC LIMIT 1
    """
    params = (fingerprint, cur_avito, *param_extra, time_limit)
    cur = await conn.execute(sql, params)
    row = await cur.fetchone()
    return dict(row) if row else None


async def notification_exists_by_fingerprint_db(
    get_recent: Callable[..., Awaitable[dict[str, Any] | None]],
    fingerprint: str,
    *,
    current_avito_id: str | None = None,
    watch_id: str | None = None,
    recipient_user_id: int | None = None,
    within_hours: int = 48,
) -> bool:
    (
        'Returns True if a notification for an item with the same fingerprint was '
        'already sent within N hours.'
    )
    row = await get_recent(
        fingerprint,
        current_avito_id=current_avito_id,
        watch_id=watch_id,
        recipient_user_id=recipient_user_id,
        within_hours=within_hours,
    )
    return bool(row)


async def get_daily_cap_db(
    get_daily_cap_with_source: Callable[[], Awaitable[tuple[int, str]]],
) -> int:
    cap, _ = await get_daily_cap_with_source()
    return cap
