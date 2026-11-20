"""Database slice 37: Listing touching, events, and evaluation persistence."""

from collections.abc import Awaitable, Callable
import json
from typing import Any
import aiosqlite

from hunter.core.models import utcnow


async def touch_listing_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    avito_id: str,
) -> None:
    """Update last_seen timestamp for a listing."""
    conn = await connect()
    await conn.execute(
        "UPDATE listings SET last_seen = ? WHERE avito_id = ?",
        (utcnow(), avito_id),
    )
    await conn.commit()


async def add_event_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    avito_id: str,
    kind: str,
    payload: dict[str, Any] | None = None,
) -> None:
    """Insert an event for a listing."""
    conn = await connect()
    await conn.execute(
        "INSERT INTO listing_events(avito_id, ts, kind, payload) VALUES (?, ?, ?, ?)",
        (
            avito_id,
            utcnow(),
            kind,
            json.dumps(payload or {}, ensure_ascii=False),
        ),
    )
    await conn.commit()


async def get_listing_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    avito_id: str,
) -> dict[str, Any] | None:
    """Fetch listing row by avito_id."""
    conn = await connect()
    cur = await conn.execute("SELECT * FROM listings WHERE avito_id = ?", (avito_id,))
    row = await cur.fetchone()
    return dict(row) if row else None


async def events_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    avito_id: str,
) -> list[dict[str, Any]]:
    """Fetch all events for a listing ordered by id."""
    conn = await connect()
    cur = await conn.execute(
        "SELECT * FROM listing_events WHERE avito_id = ? ORDER BY id",
        (avito_id,),
    )
    return [dict(r) for r in await cur.fetchall()]


async def save_evaluation_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    avito_id: str,
    data: dict[str, Any],
) -> bool:
    """Save valuation result. Return False if identical to existing last evaluation."""
    conn = await connect()
    new_verdict = data.get("verdict")
    new_skip = data.get("skip_reason")

    cur = await conn.execute(
        """
        SELECT e.id, e.verdict, e.skip_reason, l.price
        FROM evaluations e
        LEFT JOIN listings l ON e.avito_id = l.avito_id
        WHERE e.avito_id = ?
        ORDER BY e.id DESC LIMIT 1
        """,
        (avito_id,),
    )
    existing = await cur.fetchone()
    if existing is not None:
        ex_verdict = existing["verdict"]
        ex_skip = existing["skip_reason"]
        ex_price = existing["price"]
        new_price = data.get("price") if data.get("price") is not None else ex_price
        if ex_verdict == new_verdict and ex_skip == new_skip and ex_price == new_price:
            await touch_listing_db(connect, avito_id)
            return False

    await conn.execute(
        """
        INSERT INTO evaluations(
            avito_id, fair_price, fair_low, fair_high, discount, ev, risk,
            verdict, reasons, skip_reason, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            avito_id,
            data.get("fair_price"),
            data.get("fair_low"),
            data.get("fair_high"),
            data.get("discount"),
            data.get("ev"),
            data.get("risk"),
            new_verdict,
            json.dumps(data.get("reasons") or [], ensure_ascii=False),
            new_skip,
            utcnow(),
        ),
    )
    await conn.commit()
    return True


async def save_extraction_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    avito_id: str,
    data: dict[str, Any],
) -> None:
    """Save extraction result for a listing."""
    conn = await connect()
    await conn.execute(
        """
        INSERT INTO extractions(
            avito_id, extractor_version, model_id, specs, condition, defects,
            confidence, source, mismatch, low_info, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            avito_id,
            data.get("extractor_version", "rules-1"),
            data.get("model_id"),
            json.dumps(data.get("specs") or {}, ensure_ascii=False),
            data.get("condition"),
            json.dumps(data.get("defects") or [], ensure_ascii=False),
            data.get("confidence"),
            data.get("source"),
            1 if data.get("mismatch") else 0,
            1 if data.get("low_info") else 0,
            utcnow(),
        ),
    )
    await conn.commit()


async def notification_exists_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    key: str,
) -> bool:
    """Check if notification idempotency key already exists."""
    conn = await connect()
    cur = await conn.execute(
        "SELECT 1 FROM notifications WHERE idempotency_key = ?",
        (key,),
    )
    return await cur.fetchone() is not None


async def _record_legacy_notification(
    conn: aiosqlite.Connection,
    avito_id: str,
    channel: str,
    key: str,
    verdict: str,
    actual_sent_at: str | None,
    status: str,
    attempts: int,
    next_try_at: str | None,
    payload: str | None,
    owner_user_id: int | None,
    recipient_user_id: int | None,
    photo_key: str | None,
    now_ts: str,
) -> None:
    await conn.execute(
        """
        INSERT INTO notifications(
            avito_id, channel, idempotency_key, verdict, sent_at,
            status, attempts, next_try_at, payload, created_at,
            owner_user_id, recipient_user_id, photo_key
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(idempotency_key) DO UPDATE SET
            channel = excluded.channel,
            verdict = excluded.verdict,
            sent_at = excluded.sent_at,
            status = excluded.status,
            attempts = excluded.attempts,
            next_try_at = excluded.next_try_at,
            payload = excluded.payload,
            photo_key = COALESCE(excluded.photo_key, notifications.photo_key),
            owner_user_id = COALESCE(excluded.owner_user_id, notifications.owner_user_id),
            recipient_user_id = COALESCE(excluded.recipient_user_id, notifications.recipient_user_id)
        """,
        (
            avito_id, channel, key, verdict, actual_sent_at,
            status, attempts, next_try_at, payload, now_ts,
            owner_user_id, recipient_user_id, photo_key,
        ),
    )


async def record_notification_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    avito_id: str,
    channel: str,
    key: str,
    verdict: str,
    sent_at: str | None = None,
    status: str = "sent",
    attempts: int = 1,
    next_try_at: str | None = None,
    payload: str | None = None,
    owner_user_id: int | None = None,
    recipient_user_id: int | None = None,
    photo_key: str | None = None,
) -> None:
    """Record or update a notification delivery status."""
    conn = await connect()
    cur = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name = 'notifications'"
    )
    if not await cur.fetchone():
        return
    actual_sent_at = sent_at if status != "pending" else None
    if actual_sent_at is None and status == "sent":
        actual_sent_at = utcnow()
    now_ts = utcnow()
    await _record_legacy_notification(
        conn, avito_id, channel, key, verdict, actual_sent_at,
        status, attempts, next_try_at, payload, owner_user_id,
        recipient_user_id, photo_key, now_ts,
    )
    await conn.commit()

