"""Database slice 28: Daily cap, night budget, and memory logging operations."""

from collections.abc import Awaitable, Callable
from typing import Any
import aiosqlite

from hunter.core.config import get_settings
from hunter.core.models import now_utc


async def get_daily_cap_with_source_db(
    get_runtime: Callable[[str], Awaitable[str | None]],
) -> tuple[int, str]:
    val = await get_runtime("budget_daily_cap")
    if val:
        try:
            c = int(val)
            if c > 0:
                return c, "runtime (БД: budget_daily_cap)"
        except ValueError:
            pass
    settings = get_settings()
    v2 = getattr(settings, "public_v2", None)
    if v2 and getattr(v2, "enabled", False):
        return 5000, "public_v2 (5000 на активный слот/IP)"
    b = getattr(settings, "budget", {})
    if isinstance(b, dict) and "daily_cap" in b:
        try:
            return int(b["daily_cap"]), "settings.yaml (по умолчанию: budget.daily_cap)"
        except (ValueError, TypeError):
            pass
    rb = getattr(settings, "request_budget", None)
    if rb and hasattr(rb, "daily_cap"):
        return int(rb.daily_cap), "settings.yaml (request_budget.daily_cap)"
    return 900, "default (900)"


async def set_daily_cap_db(
    set_runtime: Callable[[str, str], Awaitable[None]],
    cap: int,
) -> None:
    await set_runtime("budget_daily_cap", str(cap))


async def get_night_enabled_with_source_db(
    get_runtime: Callable[[str], Awaitable[str | None]],
) -> tuple[bool, str]:
    val = await get_runtime("budget_night_enabled")
    if val is not None and val != "":
        return val in ("1", "true", "True"), "runtime (БД: budget_night_enabled)"
    settings = get_settings()
    rb = getattr(settings, "request_budget", None)
    if rb and hasattr(rb, "night_enabled"):
        return bool(rb.night_enabled), "settings.yaml (request_budget.night_enabled)"
    return True, "default (True)"


async def get_night_enabled_db(
    get_night_enabled_with_source: Callable[[], Awaitable[tuple[bool, str]]],
) -> bool:
    enabled, _ = await get_night_enabled_with_source()
    return enabled


async def set_night_enabled_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    set_runtime: Callable[[str, str], Awaitable[None]],
    enabled: bool | None,
) -> None:
    if enabled is None:
        conn = await connect()
        await conn.execute("DELETE FROM runtime WHERE key = 'budget_night_enabled'")
        await conn.commit()
    else:
        await set_runtime("budget_night_enabled", "1" if enabled else "0")


async def log_memory_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    python_rss_mb: float,
    node_rss_mb: float,
    chromium_rss_mb: float,
    total_rss_mb: float,
    ts: str | None = None,
) -> None:
    if ts is None:
        ts = now_utc().isoformat()
    conn = await connect()
    await conn.execute(
        """
        INSERT INTO memory_log (ts, python_rss_mb, node_rss_mb, chromium_rss_mb, total_rss_mb)
        VALUES (?, ?, ?, ?, ?)
        """,
        (ts, python_rss_mb, node_rss_mb, chromium_rss_mb, total_rss_mb),
    )
    await conn.commit()


async def get_recent_memory_log_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    hours: int = 12,
) -> list[dict[str, Any]]:
    conn = await connect()
    cur = await conn.execute(
        """
        SELECT ts, python_rss_mb, node_rss_mb, chromium_rss_mb, total_rss_mb
        FROM memory_log
        WHERE datetime(ts) >= datetime('now', ? || ' hours')
        ORDER BY ts ASC
        """,
        (f"-{hours}",),
    )
    rows = await cur.fetchall()
    return [
        {
            "ts": r[0],
            "python_rss_mb": r[1],
            "node_rss_mb": r[2],
            "chromium_rss_mb": r[3],
            "total_rss_mb": r[4],
        }
        for r in rows
    ]


async def get_rolling_24h_requests_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
) -> int:
    conn = await connect()
    cur = await conn.execute(
        """
        SELECT COUNT(*)
        FROM request_log
        WHERE datetime(ts) >= datetime('now', '-24 hours')
          AND req_type != 'photo'
        """
    )
    row = await cur.fetchone()
    return row[0] or 0 if row else 0


async def get_rolling_48h_signals_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
) -> dict[str, int]:
    conn = await connect()
    cur = await conn.execute(
        """
        SELECT COUNT(*),
                SUM(CASE WHEN status = 429 THEN 1 ELSE 0 END),
                SUM(CASE WHEN status = 403 THEN 1 ELSE 0 END),
                SUM(CASE WHEN req_type = 'verification' OR error = 'captcha' THEN 1 ELSE 0 END)
        FROM request_log
        WHERE datetime(ts) >= datetime('now', '-48 hours')
          AND req_type != 'photo'
        """
    )
    row = await cur.fetchone()
    return {
        "total_reqs": row[0] or 0 if row else 0,
        "err_429": row[1] or 0 if row else 0,
        "err_403": row[2] or 0 if row else 0,
        "captcha": row[3] or 0 if row else 0,
    }


async def _compute_daily_health_stats(
    conn: aiosqlite.Connection, target_date: str
) -> tuple[int, int, int, int, int, float]:
    cur = await conn.execute(
        """
        SELECT COUNT(*),
               SUM(CASE WHEN status = 429 THEN 1 ELSE 0 END),
               SUM(CASE WHEN status = 403 THEN 1 ELSE 0 END),
               SUM(CASE WHEN req_type = 'verification' OR error = 'captcha' THEN 1 ELSE 0 END),
               SUM(CASE WHEN error = 'timeout' OR status IN (408, 504) THEN 1 ELSE 0 END)
        FROM request_log
        WHERE date(ts) = ?
          AND req_type != 'photo'
        """,
        (target_date,),
    )
    row = await cur.fetchone()
    total_reqs = row[0] or 0 if row else 0
    err_429 = row[1] or 0 if row else 0
    err_403 = row[2] or 0 if row else 0
    captcha_count = row[3] or 0 if row else 0
    timeout_count = row[4] or 0 if row else 0
    timeout_ratio = round(timeout_count / total_reqs, 4) if total_reqs > 0 else 0.0
    return total_reqs, err_429, err_403, captcha_count, timeout_count, timeout_ratio


async def _compute_daily_latency_percentiles(
    conn: aiosqlite.Connection, target_date: str
) -> tuple[float, float]:
    cur = await conn.execute(
        """
        SELECT latency_ms
        FROM request_log
        WHERE date(ts) = ?
          AND req_type LIKE '%search%'
          AND latency_ms > 0
        ORDER BY latency_ms ASC
        """,
        (target_date,),
    )
    lat_rows = await cur.fetchall()
    latencies = [r[0] for r in lat_rows]
    if latencies:
        n = len(latencies)
        median_ms = float(latencies[n // 2])
        p90_idx = min(n - 1, int(n * 0.90))
        p90_ms = float(latencies[p90_idx])
    else:
        median_ms = 0.0
        p90_ms = 0.0
    return median_ms, p90_ms


async def _upsert_health_daily_record(
    conn: aiosqlite.Connection,
    target_date: str,
    total_reqs: int,
    err_429: int,
    err_403: int,
    captcha_count: int,
    timeout_count: int,
    timeout_ratio: float,
    median_ms: float,
    p90_ms: float,
) -> None:
    now_str = now_utc().isoformat()
    await conn.execute(
        """
        INSERT INTO health_daily (
            date, total_reqs, err_429, err_403, captcha_count,
            timeout_count, timeout_ratio, latency_median_ms, latency_p90_ms,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(date) DO UPDATE SET
            total_reqs = excluded.total_reqs,
            err_429 = excluded.err_429,
            err_403 = excluded.err_403,
            captcha_count = excluded.captcha_count,
            timeout_count = excluded.timeout_count,
            timeout_ratio = excluded.timeout_ratio,
            latency_median_ms = excluded.latency_median_ms,
            latency_p90_ms = excluded.latency_p90_ms,
            updated_at = excluded.updated_at
        """,
        (
            target_date,
            total_reqs,
            err_429,
            err_403,
            captcha_count,
            timeout_count,
            timeout_ratio,
            median_ms,
            p90_ms,
            now_str,
            now_str,
        ),
    )
    await conn.commit()


async def record_daily_health_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    target_date: str | None = None,
) -> dict[str, Any]:
    """Calculates and persists daily quality and health signals in health_daily."""
    conn = await connect()
    if not target_date:
        cur_date_row = await (await conn.execute("SELECT date('now')")).fetchone()
        target_date = cur_date_row[0] if cur_date_row else now_utc().strftime("%Y-%m-%d")

    (
        total_reqs,
        err_429,
        err_403,
        captcha_count,
        timeout_count,
        timeout_ratio,
    ) = await _compute_daily_health_stats(conn, target_date)
    median_ms, p90_ms = await _compute_daily_latency_percentiles(conn, target_date)

    await _upsert_health_daily_record(
        conn,
        target_date,
        total_reqs,
        err_429,
        err_403,
        captcha_count,
        timeout_count,
        timeout_ratio,
        median_ms,
        p90_ms,
    )

    return {
        "date": target_date,
        "total_reqs": total_reqs,
        "err_429": err_429,
        "err_403": err_403,
        "captcha_count": captcha_count,
        "timeout_count": timeout_count,
        "timeout_ratio": timeout_ratio,
        "latency_median_ms": median_ms,
        "latency_p90_ms": p90_ms,
    }
