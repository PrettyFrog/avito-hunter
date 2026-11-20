"""Database slice 39: Notification queue processing and listing evaluations."""

from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any
import aiosqlite

from hunter.core.models import now_utc, utcnow


async def get_pending_notifications_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    now_str: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch pending notifications ready for delivery."""
    conn = await connect()
    cur = await conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('notifications', 'deliveries')")
    tables = {r[0] for r in await cur.fetchall()}
    now_val = now_str or utcnow()
    if "deliveries" in tables:
        cur = await conn.execute(
            """
            SELECT d.*, e.avito_id FROM deliveries d
            JOIN events e ON d.event_id = e.id
            LEFT JOIN users u ON d.user_id = u.id
            WHERE d.status = 'pending'
              AND d.available_at <= ?
              AND (d.user_id IS NULL OR u.status = 'active')
            ORDER BY d.id ASC
            """,
            (now_val,),
        )
        return [dict(r) for r in await cur.fetchall()]
    if "notifications" in tables:
        cur = await conn.execute(
            """
            SELECT n.* FROM notifications n
            LEFT JOIN users u ON n.recipient_user_id = u.id
            WHERE n.status = 'pending'
              AND (n.next_try_at IS NULL OR n.next_try_at <= ?)
              AND (n.recipient_user_id IS NULL OR u.status = 'active')
            ORDER BY n.id ASC
            """,
            (now_val,),
        )
        return [dict(r) for r in await cur.fetchall()]
    return []


async def mark_notification_sent_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    notification_id: int,
    sent_at: str,
    channel: str | None = None,
) -> None:
    """Mark a notification as successfully sent."""
    conn = await connect()
    if channel:
        await conn.execute(
            "UPDATE notifications SET status = 'sent', sent_at = ?, channel = ? WHERE id = ?",
            (sent_at, channel, notification_id),
        )
    else:
        await conn.execute(
            "UPDATE notifications SET status = 'sent', sent_at = ? WHERE id = ?",
            (sent_at, notification_id),
        )
    await conn.commit()


async def mark_notification_expired_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    notification_id: int,
) -> None:
    """Mark a notification as expired."""
    conn = await connect()
    await conn.execute(
        "UPDATE notifications SET status = 'failed_expired' WHERE id = ?",
        (notification_id,),
    )
    await conn.commit()


async def get_watch_hits_24h_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    watchlist_id: str,
) -> int:
    """Return count of active hits for watchlist in past 24 hours."""
    cutoff = (now_utc() - timedelta(hours=24)).isoformat()
    conn = await connect()
    cur = await conn.execute(
        'SELECT COUNT(*) as cnt FROM listings WHERE watchlist_id = ? AND '
            '(last_seen >= ? OR first_seen >= ?)',
        (watchlist_id, cutoff, cutoff),
    )
    row = await cur.fetchone()
    return int(row["cnt"]) if row else 0


async def update_notification_retry_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    notification_id: int,
    attempts: int,
    next_try_at: str,
) -> None:
    """Update retry counters and schedule next attempt for notification."""
    conn = await connect()
    await conn.execute(
        "UPDATE notifications SET attempts = ?, next_try_at = ? WHERE id = ?",
        (attempts, next_try_at, notification_id),
    )
    await conn.commit()


async def latest_evaluations_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Fetch latest evaluations with listing details."""
    conn = await connect()
    cur = await conn.execute(
        """
        SELECT e.*, l.title, l.price, l.url, l.watchlist_id
        FROM evaluations e
        JOIN listings l ON l.avito_id = e.avito_id
        ORDER BY e.id DESC
        LIMIT ?
        """,
        (limit,),
    )
    return [dict(r) for r in await cur.fetchall()]


async def mark_missing_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    seen_ids: set[str],
    watchlist_id: str,
) -> None:
    """Mark listings absent in fresh search as gone if unseen for 1 day."""
    if not seen_ids:
        return
    conn = await connect()
    placeholders = ",".join("?" * len(seen_ids))
    cutoff = (now_utc() - timedelta(days=1)).replace(microsecond=0).isoformat()
    await conn.execute(
        f"""
        UPDATE listings SET status = 'gone'
        WHERE watchlist_id = ? AND status IN ('active', 'near_miss') AND avito_id NOT IN ({placeholders})
          AND last_seen < ?
        """,
        (watchlist_id, *seen_ids, cutoff),
    )
    await conn.commit()


async def get_seller_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    seller_id: str,
) -> dict[str, Any] | None:
    """Fetch seller record by seller_id."""
    conn = await connect()
    cur = await conn.execute("SELECT * FROM sellers WHERE seller_id = ?", (seller_id,))
    row = await cur.fetchone()
    return dict(row) if row else None


async def upsert_seller_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    data: dict[str, Any],
) -> None:
    """Insert or update seller metadata."""
    conn = await connect()
    await conn.execute(
        """
        INSERT INTO sellers(
            seller_id, kind, rating, reviews, registered, listings_count, fetched_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(seller_id) DO UPDATE SET
            kind = COALESCE(excluded.kind, kind),
            rating = COALESCE(excluded.rating, rating),
            reviews = COALESCE(excluded.reviews, reviews),
            registered = COALESCE(excluded.registered, registered),
            listings_count = COALESCE(excluded.listings_count, listings_count),
            fetched_at = excluded.fetched_at
        """,
        (
            data["seller_id"],
            data.get("kind"),
            data.get("rating"),
            data.get("reviews"),
            data.get("registered"),
            data.get("listings_count"),
            data.get("fetched_at") or utcnow(),
        ),
    )
    await conn.commit()


async def latest_evaluation_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    avito_id: str,
) -> dict[str, Any] | None:
    """Fetch the latest evaluation row for a listing."""
    conn = await connect()
    cur = await conn.execute(
        "SELECT * FROM evaluations WHERE avito_id = ? ORDER BY id DESC LIMIT 1",
        (avito_id,),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def market_prices_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    model_id: str,
) -> list[int]:
    """Fetch valid active prices for a model excluding skipped evaluations."""
    conn = await connect()
    cur = await conn.execute(
        """
        SELECT l.price FROM listings l
        WHERE l.status = 'active'
          AND l.price IS NOT NULL
          AND COALESCE(l.price_suspect, 0) = 0
          AND l.avito_id IN (SELECT avito_id FROM extractions WHERE model_id = ?)
          AND NOT EXISTS (
              SELECT 1 FROM evaluations e
              WHERE e.avito_id = l.avito_id
                AND e.id = (SELECT MAX(id) FROM evaluations WHERE avito_id = l.avito_id)
                AND e.verdict = 'skip'
          )
        """,
        (model_id,),
    )
    return [int(r["price"]) for r in await cur.fetchall()]


async def save_snapshot_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    model_id: str,
    stats: dict[str, Any],
    kind: str = "ask",
) -> None:
    """Save market price percentiles snapshot for a model."""
    conn = await connect()
    await conn.execute(
        """
        INSERT OR REPLACE INTO market_snapshots(
            model_id, condition, kind, ts, n, median, q10, q25, q75, q90
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            model_id,
            stats.get("condition", "used"),
            kind,
            utcnow(),
            stats.get("n"),
            stats.get("median"),
            stats.get("q10"),
            stats.get("q25"),
            stats.get("q75"),
            stats.get("q90"),
        ),
    )
    await conn.commit()


async def latest_snapshot_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    model_id: str,
) -> dict[str, Any] | None:
    """Fetch the latest market snapshot for a model."""
    conn = await connect()
    cur = await conn.execute(
        """
        SELECT * FROM market_snapshots
        WHERE model_id = ?
        ORDER BY ts DESC LIMIT 1
        """,
        (model_id,),
    )
    row = await cur.fetchone()
    return dict(row) if row else None

