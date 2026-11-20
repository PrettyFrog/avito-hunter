"""Database slice 47: Poison jobs summary, job listings, and request logging."""

from collections.abc import Awaitable, Callable
from datetime import timedelta
import json
from typing import Any
import aiosqlite

from hunter.core.models import now_utc, utcnow


async def get_poison_jobs_summary_db(conn: aiosqlite.Connection) -> tuple[int, list[str]]:
    """Get count and kinds of poison jobs."""
    cur = await conn.execute("SELECT count(*), group_concat(DISTINCT kind) FROM jobs WHERE status = 'poison'")
    row = await cur.fetchone()
    cnt = row[0] if row and row[0] else 0
    kinds_str = row[1] if row and row[1] else ""
    kinds = [k.strip() for k in kinds_str.split(",") if k.strip()]
    return cnt, kinds


async def list_jobs_db(
    conn: aiosqlite.Connection,
    status: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List recent jobs filtered by status."""
    query =(
        'SELECT id, kind, product_id, dedupe_key, status, attempts, max_attempts, '
        'last_error, updated_at FROM jobs'
    )
    params: list[Any] = []
    if status:
        query += " WHERE status = ?"
        params.append(status)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    cur = await conn.execute(query, tuple(params))
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def log_request_db(
    conn: aiosqlite.Connection,
    product_id: str | None,
    req_type: str,
    status: int | None = None,
    url: str | None = None,
    latency_ms: int | None = None,
    error: str | None = None,
) -> None:
    """Log an HTTP request result to request_log table."""
    now_str = now_utc().isoformat()
    lat = int(latency_ms) if isinstance(latency_ms, (int, float)) else 0
    stat = int(status) if isinstance(status, (int, float)) else None
    err = str(error) if isinstance(error, str) else None
    if stat is not None and stat >= 400 and not err:
        err = f"http_{stat}"
    p_id = str(product_id) if isinstance(product_id, str) else None
    r_type = str(req_type) if isinstance(req_type, str) else "unknown"
    u = str(url) if isinstance(url, str) else None
    await conn.execute(
        """
        INSERT INTO request_log (ts, product_id, req_type, status, url, latency_ms, error)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (now_str, p_id, r_type, stat, u, lat, err),
    )
    await conn.commit()


def aggregate_request_error_rows(
    rows: list[dict[str, Any]],
) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
    """Aggregate request error stats by status, watchlist, and request type."""
    by_status: dict[str, int] = {}
    by_watch: dict[str, int] = {}
    by_type: dict[str, int] = {}

    for r in rows:
        stat = r.get("status")
        err = r.get("error")
        pid = r.get("product_id") or "unknown"
        rtype = r.get("req_type") or "other"

        if stat and stat > 0:
            err_key = f"HTTP {stat}"
        elif err:
            err_key = err.split(":")[0]
        else:
            err_key = "Error"

        by_status[err_key] = by_status.get(err_key, 0) + 1
        by_watch[pid] = by_watch.get(pid, 0) + 1
        by_type[rtype] = by_type.get(rtype, 0) + 1

    by_status_sorted = dict(sorted(by_status.items(), key=lambda x: x[1], reverse=True))
    by_watch_sorted = dict(sorted(by_watch.items(), key=lambda x: x[1], reverse=True))
    return by_status_sorted, by_watch_sorted, by_type


async def get_recent_request_errors_db(
    conn: aiosqlite.Connection,
    since_hours: int = 24,
    limit: int = 150,
) -> dict[str, Any]:
    """Returns 24h error stats and event logs for admin monitoring."""
    cur = await conn.execute(
        """
        SELECT id, ts, product_id, req_type, status, error, url, latency_ms
        FROM request_log
        WHERE datetime(ts) >= datetime('now', ? || ' hours')
          AND ((status >= 400) OR (status IS NOT NULL AND status = 0) OR (error IS NOT NULL AND error != ''))
        ORDER BY id DESC
        LIMIT ?
        """,
        (f"-{since_hours}", limit),
    )
    rows = [dict(r) for r in await cur.fetchall()]
    by_status_sorted, by_watch_sorted, by_type = aggregate_request_error_rows(rows)

    return {
        "since_hours": since_hours,
        "total_count": len(rows),
        "by_status": by_status_sorted,
        "by_watch": by_watch_sorted,
        "by_type": by_type,
        "events": rows,
    }


async def get_unevaluated_stage0_listings_db(
    conn: aiosqlite.Connection,
    since_hours: int = 48,
) -> list[dict[str, Any]]:
    """Get active/near_miss listings from last N hours without evaluations."""
    cur = await conn.execute(
        """
        SELECT l.avito_id, l.url, l.title, l.price, l.watchlist_id, l.status, l.raw_path, l.first_seen
        FROM listings l
        LEFT JOIN evaluations e ON l.avito_id = e.avito_id
        WHERE e.avito_id IS NULL
          AND l.status IN ('active', 'near_miss')
          AND datetime(l.first_seen) >= datetime('now', ? || ' hours')
        """,
        (f"-{since_hours}",),
    )
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def sync_product_profiles_db(conn: aiosqlite.Connection, profiles: list[Any]) -> None:
    """Sync product profile configurations into products table."""
    now_str = now_utc().isoformat()
    for p in profiles:
        pid = getattr(p, "id", None) or str(p)
        aliases = json.dumps(getattr(getattr(p, "match", None), "aliases", []), ensure_ascii=False)
        category = getattr(p, "preset", "generic")
        state = "active" if getattr(p, "mode", "live") != "off" else "off"
        await conn.execute(
            """
            INSERT OR REPLACE INTO products(id, model_id, category, brand, aliases, msrp_new, fake_risk, version, state, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
            """,
            (
                pid,
                pid,
                category,
                None,
                aliases,
                getattr(p, "max_price", None),
                None,
                state,
                now_str,
                now_str,
            ),
        )
    await conn.commit()


async def clear_pending_vision_db(conn: aiosqlite.Connection, avito_id: str) -> None:
    """Clear pending vision analysis entry for listing."""
    await conn.execute("DELETE FROM runtime WHERE key = ?", (f"pending_vision:{avito_id}",))
    await conn.commit()


async def get_due_pending_vision_db(conn: aiosqlite.Connection, now_iso: str) -> list[dict[str, Any]]:
    """Retrieve pending vision queue items due for execution."""
    cur = await conn.execute("SELECT key, value FROM runtime WHERE key LIKE 'pending_vision:%'")
    rows = await cur.fetchall()
    due: list[dict[str, Any]] = []
    for r in rows:
        try:
            item = json.loads(r["value"])
            next_try = item.get("next_try_at")
            if not next_try or next_try <= now_iso:
                due.append(item)
        except Exception:
            continue
    return due


async def today_shadow_cards_db(
    get_runtime: Callable[[str, str], Awaitable[str]],
    feed_type: str | None = None,
) -> int:
    """Get count of shadow cards emitted today."""
    day = utcnow()[:10]
    if feed_type in ("gem", "model"):
        key = f"shadow_cards_{feed_type}:{day}"
        val = await get_runtime(key, "0")
        return int(val) if val.isdigit() else 0
    val = await get_runtime(f"shadow_cards:{day}", "0")
    return int(val) if val.isdigit() else 0


async def bump_shadow_cards_db(
    get_runtime: Callable[[str, str], Awaitable[str]],
    set_runtime: Callable[[str, str], Awaitable[None]],
    feed_type: str = "model",
) -> int:
    """Increment shadow cards emitted today."""
    day = utcnow()[:10]
    cur_total = await today_shadow_cards_db(get_runtime)
    await set_runtime(f"shadow_cards:{day}", str(cur_total + 1))
    key = f"shadow_cards_{feed_type}:{day}"
    val = await get_runtime(key, "0")
    cur_feed = int(val) if val.isdigit() else 0
    new_feed = cur_feed + 1
    await set_runtime(key, str(new_feed))
    return new_feed


async def today_product_cards_db(
    get_runtime: Callable[[str, str], Awaitable[str]],
    product_id: str,
) -> int:
    """Get count of product cards emitted today for a product."""
    day = utcnow()[:10]
    val = await get_runtime(f"product_cards:{product_id}:{day}", "0")
    return int(val) if val.isdigit() else 0


async def bump_product_cards_db(
    get_runtime: Callable[[str, str], Awaitable[str]],
    set_runtime: Callable[[str, str], Awaitable[None]],
    product_id: str,
) -> int:
    """Increment product cards emitted today for a product."""
    day = utcnow()[:10]
    cur = await today_product_cards_db(get_runtime, product_id)
    new_val = cur + 1
    await set_runtime(f"product_cards:{product_id}:{day}", str(new_val))
    return new_val


async def alerts_sent_in_past_hour_db(conn: aiosqlite.Connection) -> int:
    """Count notifications sent in past 60 minutes across all users."""
    one_hour_ago = (now_utc() - timedelta(hours=1)).isoformat()
    cur = await conn.execute(
        "SELECT count(*) FROM notifications WHERE status = 'sent' AND sent_at >= ?",
        (one_hour_ago,),
    )
    row = await cur.fetchone()
    return row[0] if row else 0


async def queue_for_digest_db(
    set_runtime: Callable[[str, str], Awaitable[None]],
    event_data: dict[str, Any],
) -> None:
    """Enqueue an event dict into runtime table for digest processing."""
    key = f"digest_queue:{event_data.get('avito_id', utcnow())}"
    await set_runtime(key, json.dumps(event_data, ensure_ascii=False))
