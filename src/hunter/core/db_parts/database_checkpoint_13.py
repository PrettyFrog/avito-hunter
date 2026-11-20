"""Database checkpoint, lifecycle and cleanup helpers (T-187)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
import shutil
from typing import Any

from loguru import logger

from hunter.core.models import now_utc, utcnow
from hunter.storage.v2_maintenance import cleanup_v2_records


async def checkpoint_db(db: Any, mode: str = "PASSIVE") -> tuple[int, int, int] | None:
    """Flushes SQLite WAL to main database. Modes: PASSIVE, FULL, RESTART, TRUNCATE."""
    if db._conn is None:
        return None
    try:
        cur = await db._conn.execute(f"PRAGMA wal_checkpoint({mode})")
        row = await cur.fetchone()
        return (row[0], row[1], row[2]) if row else None
    except Exception as exc:
        logger.debug("Database checkpoint({}) failed: {}", mode, exc)
        return None


async def close_db(db: Any) -> None:
    if db._conn is not None:
        try:
            await db._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass
        await db._conn.close()
        db._conn = None


def _prune_stale_images_dir(images_dir: Path | None, retention_days: int = 30) -> int:
    if not images_dir or not images_dir.exists() or retention_days <= 0:
        return 0
    cutoff_ts = now_utc().timestamp() - (retention_days * 86400)
    pruned = 0
    try:
        for entry in list(images_dir.iterdir()):
            if not entry.is_dir():
                continue
            try:
                mtime = entry.stat().st_mtime
                for f in entry.iterdir():
                    if f.is_file():
                        mtime = max(mtime, f.stat().st_mtime)
                if mtime < cutoff_ts:
                    shutil.rmtree(entry, ignore_errors=True)
                    pruned += 1
            except Exception:
                continue
    except Exception as exc:
        logger.debug("Images retention pruning failed: {}", exc)
    return pruned


async def _cleanup_v1_records_db(
    conn: Any, now: Any, job_days: int, req_days: int, mem_days: int, evt_days: int
) -> dict[str, int]:
    cur_tables = await conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {r[0] for r in await cur_tables.fetchall()}
    jobs_cnt = 0
    if "jobs" in tables:
        job_cutoff = (now - timedelta(days=job_days)).isoformat()
        cur = await conn.execute(
            "DELETE FROM jobs WHERE status = 'done' AND updated_at < ?", (job_cutoff,)
        )
        jobs_cnt = cur.rowcount
    req_cnt = 0
    if "request_log" in tables:
        req_cutoff = (now - timedelta(days=req_days)).isoformat()
        cur = await conn.execute("DELETE FROM request_log WHERE ts < ?", (req_cutoff,))
        req_cnt = cur.rowcount
    mem_cnt = 0
    if "memory_log" in tables:
        mem_cutoff = (now - timedelta(days=mem_days)).isoformat()
        cur = await conn.execute("DELETE FROM memory_log WHERE ts < ?", (mem_cutoff,))
        mem_cnt = cur.rowcount
    evt_cnt = 0
    if "listing_events" in tables:
        evt_cutoff = (now - timedelta(days=evt_days)).isoformat()
        cur = await conn.execute("DELETE FROM listing_events WHERE ts < ?", (evt_cutoff,))
        evt_cnt = cur.rowcount
    return {
        "jobs": jobs_cnt,
        "request_log": req_cnt,
        "memory_log": mem_cnt,
        "listing_events": evt_cnt,
    }


async def _cleanup_v2_records_db(
    conn: Any, now: Any, del_days: int, evt_days: int
) -> dict[str, int]:
    return await cleanup_v2_records(conn, now, del_days, evt_days)


async def cleanup_old_records_db(
    db: Any,
    job_retention_days: int = 30,
    request_log_retention_days: int = 14,
    memory_log_retention_days: int = 14,
    events_retention_days: int = 30,
    deliveries_retention_days: int = 14,
    images_retention_days: int = 30,
    images_dir: Path | None = None,
) -> dict[str, int]:
    """Prunes historical completed jobs, logs, and expired images."""
    conn = await db.connect()
    now = now_utc()
    stats = await _cleanup_v1_records_db(
        conn, now, job_retention_days, request_log_retention_days,
        memory_log_retention_days, events_retention_days
    )
    v2_stats = await _cleanup_v2_records_db(
        conn, now, deliveries_retention_days, events_retention_days
    )
    stats.update(v2_stats)
    await conn.commit()

    try:
        cur = await conn.execute("PRAGMA freelist_count")
        freelist_row = await cur.fetchone()
        stats["freelist_pages"] = freelist_row[0] if freelist_row else 0
        await conn.execute("PRAGMA optimize")
    except Exception:
        pass

    if images_dir is None:
        try:
            from hunter.core.config import get_settings
            images_dir = getattr(get_settings(), "images_dir", None)
        except Exception:
            images_dir = None
    stats["images_pruned"] = _prune_stale_images_dir(images_dir, images_retention_days)

    return stats


async def get_runtime_db(db: Any, key: str, default: str | None = None) -> str | None:
    conn = await db.connect()
    cur = await conn.execute("SELECT value FROM runtime WHERE key = ?", (key,))
    row = await cur.fetchone()
    return row["value"] if row else default


async def set_runtime_db(db: Any, key: str, value: str) -> None:
    async with db.write_lock:
        conn = await db.connect()
        await conn.execute(
            """
            INSERT INTO runtime(key, value) VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (key, value),
        )
        await conn.commit()


async def get_triage_verdicts_db(
    db: Any, title_hashes: list[str], product_id: str | None = None
) -> dict[str, str]:
    """Return {title_hash: verdict} for the hashes already present in the cache."""
    if not title_hashes:
        return {}
    conn = await db.connect()
    out: dict[str, str] = {}
    for i in range(0, len(title_hashes), 500):
        chunk = title_hashes[i : i + 500]
        placeholders = ",".join("?" * len(chunk))
        if product_id:
            sql = (
                "SELECT title_hash, verdict FROM loose_triage_cache "
                f"WHERE product_id = ? AND title_hash IN ({placeholders})"
            )
            params = [product_id] + chunk
        else:
            sql = (
                "SELECT title_hash, verdict FROM loose_triage_cache "
                f"WHERE title_hash IN ({placeholders})"
            )
            params = chunk
        cur = await conn.execute(sql, params)
        for row in await cur.fetchall():
            out[row["title_hash"]] = row["verdict"]
    return out


async def set_triage_verdicts_db(
    db: Any, rows: list[tuple[str, str, str | None, str | None]]
) -> None:
    """Upsert (title_hash, verdict, product_id, title) rows into the triage cache."""
    if not rows:
        return
    conn = await db.connect()
    ts = utcnow()
    await conn.executemany(
        """
        INSERT INTO loose_triage_cache(
          title_hash, verdict, product_id, title, created_at, updated_at
        )
        VALUES(?, ?, ?, ?, ?, ?)
        ON CONFLICT(product_id, title_hash) DO UPDATE SET
          verdict=excluded.verdict,
          title=excluded.title,
          updated_at=excluded.updated_at
        """,
        [(h, v, pid or "", t, ts, ts) for h, v, pid, t in rows],
    )
    await conn.commit()



def _build_user_insert_payload(
    telegram_user_id: int,
    chat_id: int | None,
    username: str | None,
    display_name: str | None,
    role: str,
    status: str,
    tier: str,
    max_products: int,
    invited_by: int | None,
    now_str: str,
    min_interval_minutes: int | None,
    max_alerts_per_hour: int | None,
) -> tuple[list[str], list[Any]]:
    cols = [
        "telegram_user_id", "chat_id", "username", "display_name", "role",
        "status", "tier", "max_products", "invited_by", "created_at", "last_seen_at",
    ]
    vals: list[Any] = [
        telegram_user_id, chat_id or telegram_user_id, username, display_name,
        role, status, tier, max_products, invited_by, now_str, now_str,
    ]
    if min_interval_minutes is not None:
        cols.append("min_interval_minutes")
        vals.append(min_interval_minutes)
    if max_alerts_per_hour is not None:
        cols.append("max_alerts_per_hour")
        vals.append(max_alerts_per_hour)
    return cols, vals


async def create_user_db(
    db: Any,
    telegram_user_id: int,
    chat_id: int | None = None,
    username: str | None = None,
    display_name: str | None = None,
    role: str = "user",
    status: str = "active",
    tier: str = "friend",
    max_products: int = 3,
    invited_by: int | None = None,
    min_interval_minutes: int | None = None,
    max_alerts_per_hour: int | None = None,
) -> int:
    conn = await db.connect()
    now_str = now_utc().isoformat()
    cols, vals = _build_user_insert_payload(
        telegram_user_id, chat_id, username, display_name, role, status, tier,
        max_products, invited_by, now_str, min_interval_minutes, max_alerts_per_hour,
    )
    placeholders = ", ".join("?" for _ in cols)
    col_names = ", ".join(cols)
    cur = await conn.execute(
        f"INSERT INTO users ({col_names}) VALUES ({placeholders})",
        vals,
    )
    await conn.commit()
    return cur.lastrowid or 0
