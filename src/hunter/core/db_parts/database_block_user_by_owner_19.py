"""Database user status, tier and support ticket helpers (T-192)."""

from __future__ import annotations

from typing import Any

from hunter.core.models import now_utc


async def _public_status_change(db: Any, user_id: int, status: str) -> bool:
    writer = getattr(db, "_public_writer", None)
    if writer is None:
        return False

    async def update(conn):
        user = await (await conn.execute(
            "SELECT role, status FROM users WHERE id = ?", (user_id,)
        )).fetchone()
        if not user or user[1] == "deleted":
            return
        if user[0] == "owner":
            if status != "bot_blocked":
                raise ValueError("Owner cannot be deleted, blocked, or paused")
            return
        if status == "bot_blocked" and user[1] == "blocked_by_owner":
            return
        await conn.execute(
            "UPDATE users SET status = ?, last_seen_at = ? WHERE id = ?",
            (status, now_utc().isoformat(), user_id),
        )

    await writer.submit(update)
    return True


async def block_user_by_owner_db(db: Any, user_id: int) -> None:
    """Blocks user explicitly by owner. Owner cannot be blocked."""
    if await _public_status_change(db, user_id, "blocked_by_owner"):
        return
    user = await db.get_user_by_id(user_id)
    if not user:
        return
    if user.get("role") == "owner":
        raise ValueError("Owner cannot be deleted, blocked, or paused")
    conn = await db.connect()
    now_str = now_utc().isoformat()
    await conn.execute(
        "UPDATE users SET status = 'blocked_by_owner', last_seen_at = ? WHERE id = ?",
        (now_str, user_id),
    )
    await conn.commit()


async def unblock_user_by_owner_db(db: Any, user_id: int) -> None:
    """Unblocks user by owner."""
    if await _public_status_change(db, user_id, "active"):
        return
    user = await db.get_user_by_id(user_id)
    if not user:
        return
    if user.get("role") == "owner":
        raise ValueError("Owner cannot be deleted, blocked, or paused")
    conn = await db.connect()
    now_str = now_utc().isoformat()
    await conn.execute(
        "UPDATE users SET status = 'active', last_seen_at = ? WHERE id = ?",
        (now_str, user_id),
    )
    await conn.commit()


async def mark_user_bot_blocked_db(db: Any, user_id: int) -> None:
    """Marks user as bot_blocked when bot is blocked in telegram.
    Owner is never marked. Does NOT overwrite blocked_by_owner.
    """
    if await _public_status_change(db, user_id, "bot_blocked"):
        return
    user = await db.get_user_by_id(user_id)
    if not user or user.get("role") == "owner":
        return
    if user.get("status") == "blocked_by_owner":
        return
    conn = await db.connect()
    now_str = now_utc().isoformat()
    await conn.execute(
        "UPDATE users SET status = 'bot_blocked', last_seen_at = ? WHERE id = ?",
        (now_str, user_id),
    )
    await conn.commit()


async def change_user_tier_db(
    db: Any,
    user_id: int,
    new_tier: str,
    min_interval_minutes: int | None = None,
    max_products: int | None = None,
    max_alerts_per_hour: int | None = None,
) -> None:
    """Changes user tier and updates default limits."""
    user = await db.get_user_by_id(user_id)
    if not user or user.get("role") == "owner":
        return
    conn = await db.connect()
    updates = ["tier = ?"]
    params: list[Any] = [new_tier]
    if min_interval_minutes is not None:
        updates.append("min_interval_minutes = ?")
        params.append(min_interval_minutes)
    if max_products is not None:
        updates.append("max_products = ?")
        params.append(max_products)
    if max_alerts_per_hour is not None:
        updates.append("max_alerts_per_hour = ?")
        params.append(max_alerts_per_hour)
    params.append(user_id)
    await conn.execute(
        f"UPDATE users SET {', '.join(updates)} WHERE id = ?",
        params,
    )
    await conn.commit()


async def create_support_ticket_db(db: Any, user_id: int, chat_id: int, text: str) -> int:
    conn = await db.connect()
    now_str = now_utc().isoformat()
    cur = await conn.execute(
        """
        INSERT INTO support_tickets (user_id, chat_id, text, status, created_at)
        VALUES (?, ?, ?, 'open', ?)
        """,
        (user_id, chat_id, text, now_str),
    )
    await conn.commit()
    return cur.lastrowid or 0


async def get_open_support_tickets_db(db: Any) -> list[dict]:
    conn = await db.connect()
    cur = await conn.execute(
        """
        SELECT st.*, u.username, u.display_name, u.telegram_user_id
        FROM support_tickets st
        LEFT JOIN users u ON st.user_id = u.id
        WHERE st.status = 'open'
        ORDER BY st.created_at ASC
        """
    )
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_support_ticket_db(db: Any, ticket_id: int) -> dict | None:
    conn = await db.connect()
    cur = await conn.execute(
        """
        SELECT st.*, u.username, u.display_name, u.telegram_user_id, u.status as user_status
        FROM support_tickets st
        LEFT JOIN users u ON st.user_id = u.id
        WHERE st.id = ?
        """,
        (ticket_id,),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def answer_support_ticket_db(
    db: Any,
    ticket_id: int,
    answered_by: int,
    response_text: str,
    status: str = "answered",
    error: str | None = None,
) -> None:
    conn = await db.connect()
    now_str = now_utc().isoformat()
    await conn.execute(
        """
        UPDATE support_tickets
        SET status = ?, answered_by = ?, response_text = ?, error = ?, answered_at = ?
        WHERE id = ?
        """,
        (status, answered_by, response_text, error, now_str, ticket_id),
    )
    await conn.commit()


async def get_user_support_tickets_count_24h_db(db: Any, user_id: int) -> int:
    conn = await db.connect()
    cur = await conn.execute(
        """
        SELECT COUNT(*) FROM support_tickets
        WHERE user_id = ? AND datetime(created_at) >= datetime('now', '-24 hours')
        """,
        (user_id,),
    )
    row = await cur.fetchone()
    return int(row[0]) if row else 0


async def log_broadcast_db(
    db: Any,
    sender_user_id: int | None,
    target_audience: str,
    text: str,
    delivered_count: int,
    failed_count: int,
) -> int:
    conn = await db.connect()
    now_str = now_utc().isoformat()
    cur = await conn.execute(
        "\n        INSERT INTO broadcasts (sender_user_id, target_audience, text, "
        "delivered_count, failed_count, created_at)\n        VALUES (?, ?, ?, ?, ?, ?)\n        ",
        (sender_user_id, target_audience, text, delivered_count, failed_count, now_str),
    )
    await conn.commit()
    return cur.lastrowid or 0


async def get_broadcast_history_db(db: Any, limit: int = 10) -> list[dict]:
    conn = await db.connect()
    cur = await conn.execute(
        """
        SELECT b.*, u.username as sender_username, u.display_name as sender_name
        FROM broadcasts b
        LEFT JOIN users u ON b.sender_user_id = u.id
        ORDER BY b.created_at DESC LIMIT ?
        """,
        (limit,),
    )
    rows = await cur.fetchall()
    return [dict(r) for r in rows]
