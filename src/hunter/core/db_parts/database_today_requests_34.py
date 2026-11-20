"""Database slice 34: Daily request metrics and price plausibility."""

import json
import sqlite3
from collections.abc import Awaitable, Callable
from typing import Any
import aiosqlite

from hunter.core.models import utcnow


async def today_requests_db(connect: Callable[[], Awaitable[aiosqlite.Connection]]) -> int:
    """Return requests count for today."""
    day = utcnow()[:10]
    conn = await connect()
    cur = await conn.execute("SELECT requests FROM metrics WHERE day = ?", (day,))
    row = await cur.fetchone()
    return int(row["requests"]) if row else 0


async def get_metric_today_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    field: str,
) -> int:
    """Return today's value for a metric field."""
    day = utcnow()[:10]
    conn = await connect()
    try:
        cur = await conn.execute(f"SELECT {field} FROM metrics WHERE day = ?", (day,))
        row = await cur.fetchone()
        return int(row[0]) if (row and row[0] is not None) else 0
    except Exception:
        return 0


async def record_search_metrics_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    hits_total: int,
    hits_no_price: int,
    anchor_fallback: int = 0,
) -> None:
    """Increment hits and anchor fallback counters for today."""
    day = utcnow()[:10]
    conn = await connect()
    await conn.execute(
        """
        INSERT INTO metrics(day, hits_total, hits_no_price, anchor_fallback) VALUES(?, ?, ?, ?)
        ON CONFLICT(day) DO UPDATE SET
            hits_total = hits_total + excluded.hits_total,
            hits_no_price = hits_no_price + excluded.hits_no_price,
            anchor_fallback = anchor_fallback + excluded.anchor_fallback
        """,
        (day, hits_total, hits_no_price, anchor_fallback),
    )
    await conn.commit()


async def get_daily_metrics_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    day: str | None = None,
) -> dict[str, Any] | None:
    """Get all metric counters for a given day."""
    target_day = day or utcnow()[:10]
    conn = await connect()
    cur = await conn.execute("SELECT * FROM metrics WHERE day = ?", (target_day,))
    row = await cur.fetchone()
    return dict(row) if row else None


async def find_implausible_prices_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    threshold: int = 1_000_000,
) -> list[dict[str, Any]]:
    """Find listings with implausible prices above threshold."""
    conn = await connect()
    cur = await conn.execute(
        'SELECT avito_id, title, price, watchlist_id FROM listings WHERE price > '
            '? ORDER BY price DESC',
        (threshold,),
    )
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def fix_implausible_prices_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    threshold: int = 1_000_000,
) -> int:
    """Mark listings with implausible prices as suspect and clear price."""
    conn = await connect()
    cur = await conn.execute(
        "UPDATE listings SET price = NULL, price_suspect = 1 WHERE price > ?",
        (threshold,),
    )
    await conn.commit()
    return cur.rowcount


async def insert_new_listing_db(
    conn: aiosqlite.Connection,
    avito_id: str,
    data: dict[str, Any],
    params: str | None,
    now: str,
    add_event: Callable[[str, str, dict[str, Any] | None], Awaitable[None]] | None = None,
) -> bool:
    """Insert a new listing record and record its first-seen event."""
    try:
        await conn.execute(
            """
            INSERT INTO listings(
                avito_id, url, first_seen, last_seen, status, title, price,
                description, params, city, delivery, seller_id, seller_type,
                watchlist_id, raw_path, fingerprint, price_suspect, published_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                avito_id,
                data["url"],
                now,
                now,
                data.get("status", "active"),
                data.get("title"),
                data.get("price"),
                data.get("description"),
                params,
                data.get("city"),
                1 if data.get("delivery") else 0 if data.get("delivery") is not None else None,
                data.get("seller_id"),
                data.get("seller_type"),
                data.get("watchlist_id"),
                data.get("raw_path"),
                data.get("fingerprint"),
                1 if data.get("price_suspect") else 0,
                data.get("published_at"),
            ),
        )
    except (sqlite3.IntegrityError, aiosqlite.IntegrityError):
        return False

    payload_json = json.dumps({"price": data.get("price")}, ensure_ascii=False)
    await conn.execute(
        "INSERT INTO listing_events(avito_id, ts, kind, payload) VALUES (?, ?, ?, ?)",
        (avito_id, now, "first_seen", payload_json),
    )
    if data.get("price") is not None:
        await conn.execute(
            "INSERT OR IGNORE INTO price_history(listing_id, ts, price) VALUES (?, ?, ?)",
            (avito_id, now, data["price"]),
        )
    await conn.commit()
    return True


async def update_existing_listing_db(
    conn: aiosqlite.Connection,
    avito_id: str,
    data: dict[str, Any],
    params: str | None,
    now: str,
) -> None:
    """Update fields on an existing listing record."""
    await conn.execute(
        """
        UPDATE listings SET
            url = COALESCE(?, url),
            last_seen = ?,
            status = ?,
            title = COALESCE(?, title),
            price = COALESCE(?, price),
            description = COALESCE(?, description),
            params = COALESCE(?, params),
            city = COALESCE(?, city),
            delivery = COALESCE(?, delivery),
            seller_id = COALESCE(?, seller_id),
            seller_type = COALESCE(?, seller_type),
            watchlist_id = COALESCE(?, watchlist_id),
            raw_path = COALESCE(?, raw_path),
            fingerprint = COALESCE(?, fingerprint),
            price_suspect = CASE WHEN ? IS NOT NULL THEN ? ELSE price_suspect END,
            published_at = COALESCE(?, published_at)
        WHERE avito_id = ?
        """,
        (
            data.get("url"),
            now,
            data.get("status", "active"),
            data.get("title"),
            data.get("price"),
            data.get("description"),
            params,
            data.get("city"),
            1 if data.get("delivery") else 0 if data.get("delivery") is not None else None,
            data.get("seller_id"),
            data.get("seller_type"),
            data.get("watchlist_id"),
            data.get("raw_path"),
            data.get("fingerprint"),
            1 if data.get("price_suspect") else (0 if "price_suspect" in data else None),
            1 if data.get("price_suspect") else 0,
            data.get("published_at"),
            avito_id,
        ),
    )


async def record_listing_price_change_db(
    conn: aiosqlite.Connection,
    avito_id: str,
    data: dict[str, Any],
    old_price: int | None,
    now: str,
    add_event: Callable[[str, str, dict[str, Any] | None], Awaitable[None]] | None = None,
) -> str:
    """Record price change events and history for an updated listing."""
    change = "seen"
    if data.get("price") is not None and old_price is not None and data["price"] != old_price:
        await conn.execute(
            "INSERT OR IGNORE INTO price_history(listing_id, ts, price) VALUES (?, ?, ?)",
            (avito_id, now, data["price"]),
        )
        kind = "price_drop" if data["price"] < old_price else "price_change"
        payload_json = json.dumps({"from": old_price, "to": data["price"]}, ensure_ascii=False)
        await conn.execute(
            "INSERT INTO listing_events(avito_id, ts, kind, payload) VALUES (?, ?, ?, ?)",
            (avito_id, now, kind, payload_json),
        )
        change = kind
    elif data.get("price") is not None and old_price is None:
        await conn.execute(
            "INSERT OR IGNORE INTO price_history(listing_id, ts, price) VALUES (?, ?, ?)",
            (avito_id, now, data["price"]),
        )
        payload_json = json.dumps({"price": data["price"]}, ensure_ascii=False)
        await conn.execute(
            "INSERT INTO listing_events(avito_id, ts, kind, payload) VALUES (?, ?, ?, ?)",
            (avito_id, now, "price_set", payload_json),
        )
    await conn.commit()
    return change


