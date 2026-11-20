"""Database slice 54: Shadow overlaps, autostop runtime, and 7-day field coverage baseline."""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any
import aiosqlite
from loguru import logger


async def get_recent_shadow_overlaps_db(
    conn: aiosqlite.Connection,
    watch_id: str,
    limit: int = 2,
) -> list[float | None]:
    """Retrieve recent overlap percentages for a watch ID."""
    cur = await conn.execute(
        "SELECT id_overlap_pct FROM shadow_fetch WHERE watch_id = ? ORDER BY id DESC LIMIT ?",
        (watch_id, limit),
    )
    rows = await cur.fetchall()
    return [r[0] for r in rows]


async def get_shadow_autostop_db(
    get_runtime: Callable[[str], Awaitable[str | None]],
) -> datetime | None:
    """Get timestamp when shadow autostop expires."""
    val = await get_runtime("shadow_autostop_until")
    if not val:
        return None
    try:
        return datetime.fromisoformat(val)
    except Exception:
        return None


async def set_shadow_autostop_db(
    set_runtime: Callable[[str, str], Awaitable[None]],
    until: datetime,
) -> None:
    """Set timestamp until which shadow autostop remains active."""
    await set_runtime("shadow_autostop_until", until.isoformat())


async def get_7day_field_coverage_baseline_db(conn: aiosqlite.Connection) -> dict[str, float]:
    """Retrieve field coverage baseline from listings over past 7 days."""
    try:
        cur = await conn.execute(
            """
            SELECT COUNT(*),
                   COUNT(title),
                   COUNT(price),
                   COUNT(url),
                   COUNT(seller_type)
            FROM listings
            WHERE datetime(first_seen) >= datetime('now', '-7 days')
            """
        )
        row = await cur.fetchone()
        if row and row[0] and row[0] >= 10:
            total = float(row[0])
            return {
                "title": round((row[1] or 0) / total, 4),
                "price": round((row[2] or 0) / total, 4),
                "url": round((row[3] or 0) / total, 4),
                "seller_type": round((row[4] or 0) / total, 4),
                "price_state": 0.95,
                "date": 0.50,
            }
    except Exception as exc:
        logger.debug("Could not query 7-day field baseline: {}", exc)

    return {
        "title": 0.98,
        "price": 0.90,
        "url": 0.98,
        "seller_type": 0.0,
        "price_state": 0.95,
        "date": 0.50,
    }


_db_singleton: Any = None


def get_db_singleton(db_cls: type) -> Any:
    """Retrieve or initialize singleton database instance."""
    global _db_singleton
    if _db_singleton is None:
        _db_singleton = db_cls()
    return _db_singleton


@asynccontextmanager
async def lifespan_db_ctx(db_cls: type) -> AsyncIterator[Any]:
    """Lifespan context manager yielding database instance."""
    db = get_db_singleton(db_cls)
    await db.connect()
    try:
        yield db
    finally:
        await db.close()
