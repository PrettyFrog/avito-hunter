"""Database slice 23: Access request resolution and user reporting operations."""

from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any
import aiosqlite

from hunter.core.models import now_utc


async def resolve_access_request_atomic_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    request_id: int,
    status: str,
    decided_by: int,
) -> bool:
    """Atomically resolve a pending access request.
    Returns True if transitioned from 'pending' to target status, False if already resolved.
    """
    conn = await connect()
    cur = await conn.execute(
        """
        UPDATE access_requests
        SET status = ?, decided_by = ?
        WHERE id = ? AND status = 'pending'
        """,
        (status, decided_by, request_id),
    )
    await conn.commit()
    return (cur.rowcount or 0) > 0


async def get_user_findings_7d_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    user_id: int,
) -> int:
    """Returns delivered alerts count in the last 7 days for recipient_user_id."""
    conn = await connect()
    cur = await conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('notifications', 'deliveries')")
    tables = {r[0] for r in await cur.fetchall()}
    since = (now_utc() - timedelta(days=7)).isoformat()
    if "deliveries" in tables:
        cur = await conn.execute(
            "SELECT COUNT(*) FROM deliveries WHERE user_id = ? AND status = 'sent' AND created_at >= ?",
            (user_id, since),
        )
        row = await cur.fetchone()
        return int(row[0]) if row else 0
    if "notifications" in tables:
        cur = await conn.execute(
            """
            SELECT COUNT(*) 
            FROM notifications 
            WHERE recipient_user_id = ? AND status = 'sent' AND (sent_at >= ? OR created_at >= ?)
            """,
            (user_id, since, since),
        )
        row = await cur.fetchone()
        return int(row[0]) if row else 0
    return 0


async def get_user_feedback_counts_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    user_id: int,
) -> tuple[int, int]:
    """Returns (thumbs_up, thumbs_down) feedback counts for a user."""
    conn = await connect()
    cur = await conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='feedback'")
    if not await cur.fetchone():
        return 0, 0
    cur = await conn.execute(
        """
        SELECT feedback, COUNT(*) 
        FROM feedback 
        WHERE owner_user_id = ? 
        GROUP BY feedback
        """,
        (user_id,),
    )
    rows = await cur.fetchall()
    up = 0
    down = 0
    for row in rows:
        code = str(row[0]).lower()
        count = int(row[1])
        if code in ("ok", "target_model", "thumbs_up", "good", "up"):
            up += count
        elif code in ("wrong_model", "fake", "expensive", "thumbs_down", "bad", "down"):
            down += count
    return (up, down)


async def get_requests_by_tier_24h_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
) -> dict[str, int]:
    """Returns request counts aggregated by user tier over the past 24 hours."""
    conn = await connect()
    since = (now_utc() - timedelta(hours=24)).isoformat()
    cur = await conn.execute(
        """
        SELECT COALESCE(u.tier, 'owner') as tier, COUNT(*)
        FROM request_log r
        LEFT JOIN products p ON r.product_id = p.id
        LEFT JOIN users u ON p.owner_user_id = u.id
        WHERE r.ts >= ?
        GROUP BY tier
        """,
        (since,),
    )
    rows = await cur.fetchall()
    result = {"owner": 0, "family": 0, "friend": 0}
    for r in rows:
        t = str(r[0])
        result[t] = int(r[1])
    return result


async def get_alerts_by_tier_24h_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
) -> dict[str, int]:
    """Returns delivered alert counts aggregated by user tier over the past 24 hours."""
    conn = await connect()
    since = (now_utc() - timedelta(hours=24)).isoformat()
    cur = await conn.execute(
        """
        SELECT COALESCE(u.tier, 'owner') as tier, COUNT(*)
        FROM notifications n
        LEFT JOIN users u ON n.recipient_user_id = u.id
        WHERE n.status = 'sent' AND (n.sent_at >= ? OR n.created_at >= ?)
        GROUP BY tier
        """,
        (since, since),
    )
    rows = await cur.fetchall()
    result = {"owner": 0, "family": 0, "friend": 0}
    for r in rows:
        t = str(r[0])
        result[t] = int(r[1])
    return result


async def alerts_sent_in_past_hour_for_user_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    user_id: int,
) -> int:
    """Returns count of alerts delivered to user in the past 1 hour."""
    conn = await connect()
    since = (now_utc() - timedelta(hours=1)).isoformat()
    cur = await conn.execute(
        """
        SELECT COUNT(*) 
        FROM notifications 
        WHERE recipient_user_id = ? AND status = 'sent' AND (sent_at >= ? OR created_at >= ?)
        """,
        (user_id, since, since),
    )
    row = await cur.fetchone()
    return int(row[0]) if row else 0


async def count_recent_add_attempts_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    user_id: int | None = None,
    hours: int = 24,
) -> int:
    """Counts add attempts. If user_id is None, counts across all non-owners."""
    conn = await connect()
    since = (now_utc() - timedelta(hours=hours)).isoformat()
    if user_id is not None:
        cur = await conn.execute(
            'SELECT COUNT(*) FROM user_events WHERE user_id = ? AND kind = '
                "'add_attempt' AND ts >= ?",
            (user_id, since),
        )
    else:
        cur = await conn.execute(
            """
            SELECT COUNT(*) FROM user_events e
            JOIN users u ON e.user_id = u.id
            WHERE u.role != 'owner' AND e.kind = 'add_attempt' AND e.ts >= ?
            """,
            (since,),
        )
    row = await cur.fetchone()
    return int(row[0]) if row else 0


async def enqueue_digest_item_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    user_id: int,
    avito_id: str,
    payload: str,
    reason: str,
) -> None:
    """Enqueues an alert for digest delivery (quiet_hours or hourly_cap)."""
    conn = await connect()
    now_str = now_utc().isoformat()
    await conn.execute(
        """
        INSERT INTO digest_queue (user_id, avito_id, payload, reason, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (user_id, avito_id, payload, reason, now_str),
    )
    await conn.commit()
