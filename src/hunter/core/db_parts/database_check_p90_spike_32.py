"""Database slice 32: Latency spike checks and pending product dialog state."""

import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import aiosqlite

from hunter.core.config import get_settings
from hunter.core.models import now_utc, utcnow


async def check_p90_spike_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    target_date: str | None = None,
) -> tuple[bool, float, float]:
    """Checks if today's search latency p90 doubled day-over-day."""
    conn = await connect()
    if not target_date:
        cur_date_row = await (await conn.execute("SELECT date('now')")).fetchone()
        target_date = cur_date_row[0] if cur_date_row else now_utc().strftime("%Y-%m-%d")

    cur = await conn.execute(
        "SELECT latency_p90_ms, total_reqs FROM health_daily WHERE date = ?",
        (target_date,),
    )
    row_today = await cur.fetchone()
    if not row_today or (row_today[1] or 0) < 5 or (row_today[0] or 0) <= 0:
        return False, 0.0, 0.0

    p90_today = float(row_today[0])

    cur = await conn.execute(
        "SELECT latency_p90_ms, total_reqs FROM health_daily WHERE date = date(?, '-1 day')",
        (target_date,),
    )
    row_yest = await cur.fetchone()
    if not row_yest or (row_yest[1] or 0) < 5 or (row_yest[0] or 0) <= 0:
        return False, p90_today, 0.0

    p90_yest = float(row_yest[0])
    if p90_yest > 0 and p90_today >= 2.0 * p90_yest:
        return True, p90_today, p90_yest

    return False, p90_today, p90_yest


async def save_pending_product_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    chat_id: int,
    step: str,
    payload: dict[str, Any],
    ttl_hours: int = 24,
    owner_user_id: int | None = None,
) -> None:
    """Stores pending product addition dialog state with 24h TTL (PR-3.5 B)."""
    conn = await connect()
    now = now_utc()
    expires = now + timedelta(hours=ttl_hours)
    payload_str = json.dumps(payload, ensure_ascii=False)
    await conn.execute(
        """
        INSERT INTO pending_products (chat_id, step, payload, created_at, updated_at, expires_at, owner_user_id)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
          step = excluded.step,
          payload = excluded.payload,
          updated_at = excluded.updated_at,
          expires_at = excluded.expires_at,
          owner_user_id = coalesce(excluded.owner_user_id, pending_products.owner_user_id)
        """,
        (chat_id, step, payload_str, now.isoformat(), now.isoformat(), expires.isoformat(), owner_user_id),
    )
    await conn.commit()


async def get_pending_product_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    chat_id: int,
) -> dict[str, Any] | None:
    """Retrieves active pending product addition dialog state if not expired."""
    conn = await connect()
    now_str = now_utc().isoformat()
    cur = await conn.execute(
        "SELECT step, payload, expires_at, owner_user_id FROM pending_products WHERE chat_id = ?",
        (chat_id,),
    )
    row = await cur.fetchone()
    if not row:
        return None
    if row["expires_at"] < now_str:
        await conn.execute("DELETE FROM pending_products WHERE chat_id = ?", (chat_id,))
        await conn.commit()
        return None
    try:
        payload = json.loads(row["payload"])
    except Exception:
        payload = {}
    return {
        "step": row["step"],
        "payload": payload,
        "expires_at": row["expires_at"],
        "owner_user_id": row["owner_user_id"],
    }


async def delete_pending_product_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    chat_id: int,
) -> None:
    """Deletes pending product addition dialog state."""
    conn = await connect()
    await conn.execute("DELETE FROM pending_products WHERE chat_id = ?", (chat_id,))
    await conn.commit()


async def record_feedback_entry_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    avito_id: str,
    feedback: str,
    product_id: str | None = None,
    owner_user_id: int | None = None,
) -> None:
    """Records user feedback button event into feedback table (PR-3.5 C)."""
    conn = await connect()
    await conn.execute(
        'INSERT INTO feedback (avito_id, product_id, feedback, created_at, '
            'owner_user_id) VALUES (?, ?, ?, ?, ?)',
        (avito_id, product_id, feedback, now_utc().isoformat(), owner_user_id),
    )
    await conn.commit()


async def get_feedback_stats_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    product_id: str | None = None,
) -> dict[str, Any]:
    """Calculates feedback statistics and precision (PR-3.5 C/D)."""
    conn = await connect()
    if product_id:
        cur = await conn.execute(
            """
            SELECT feedback, COUNT(*)
            FROM feedback
            WHERE product_id = ?
            GROUP BY feedback
            """,
            (product_id,),
        )
    else:
        cur = await conn.execute(
            """
            SELECT feedback, COUNT(*)
            FROM feedback
            GROUP BY feedback
            """
        )
    rows = await cur.fetchall()
    counts: dict[str, int] = {}
    for r in rows:
        counts[r[0]] = r[1]

    target_cnt = counts.get("target_model", 0) + counts.get("ok", 0)
    wrong_cnt = counts.get("wrong_model", 0) + counts.get("fake", 0)
    total = sum(counts.values())
    precision = (target_cnt / (target_cnt + wrong_cnt) * 100.0) if (target_cnt + wrong_cnt) > 0 else 100.0
    return {
        "counts": counts,
        "target_model": target_cnt,
        "wrong_model": wrong_cnt,
        "total": total,
        "precision_pct": round(precision, 1),
    }


async def is_paused_db(
    get_runtime: Callable[..., Awaitable[str | None]],
    set_runtime: Callable[[str, str], Awaitable[None]],
) -> bool:
    if (get_settings().data_dir / "pause.flag").exists():
        return True
    if (await get_runtime("paused", "0")) == "1":
        return True
    canary_until = await get_runtime("canary_paused_until", "")
    if canary_until:
        try:
            until_dt = datetime.fromisoformat(canary_until)
            if now_utc() < until_dt:
                return True
            # Expired: clear auto-pause
            await set_runtime("canary_paused_until", "")
        except Exception:
            await set_runtime("canary_paused_until", "")
    return False


async def clear_all_pauses_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
) -> None:
    """Clear only expired watch-specific canary pause state.

    Preserves active watch quarantine deadlines, permanent pauses, global circuit_until,
    paused, and captcha flags so restarts do not bypass safety protections.
    """
    conn = await connect()
    cur = await conn.execute("SELECT key, value FROM runtime WHERE key LIKE 'watch_paused_until:%'")
    rows = await cur.fetchall()
    now = now_utc()
    keys_to_delete = []
    for k, v in rows:
        if not v:
            keys_to_delete.append(k)
        elif v == "permanent":
            continue
        else:
            try:
                dt = datetime.fromisoformat(v)
                if dt.tzinfo is None and now.tzinfo is not None:
                    dt = dt.replace(tzinfo=UTC)
                if dt <= now:
                    keys_to_delete.append(k)
            except Exception:
                keys_to_delete.append(k)
    for k in keys_to_delete:
        await conn.execute("DELETE FROM runtime WHERE key = ?", (k,))
    await conn.execute("DELETE FROM runtime WHERE key LIKE 'canary_consecutive_fails%'")
    await conn.commit()


async def set_listing_status_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    avito_id: str,
    status: str,
) -> None:
    conn = await connect()
    await conn.execute("UPDATE listings SET status = ? WHERE avito_id = ?", (status, avito_id))
    await conn.commit()


async def bump_metric_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    field: str,
    n: int = 1,
) -> None:
    day = utcnow()[:10]
    conn = await connect()
    await conn.execute(
        f"INSERT INTO metrics(day, {field}) VALUES(?, ?) "
        f"ON CONFLICT(day) DO UPDATE SET {field} = {field} + excluded.{field}",
        (day, n),
    )
    await conn.commit()
