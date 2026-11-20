"""Database slice 41: Product catalog sync, FTS search, and listing images."""

from collections.abc import Awaitable, Callable
import asyncio
import json
import sqlite3
from typing import Any
import aiosqlite

from hunter.core.models import now_utc, utcnow


async def sync_products_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    catalog_dict: dict[str, dict[str, Any]],
) -> None:
    """Sync external catalog specs into products table."""
    conn = await connect()
    now_str = now_utc().isoformat()
    for model_id, spec in catalog_dict.items():
        aliases = json.dumps(spec.get("aliases", []), ensure_ascii=False)
        await conn.execute(
            """
            INSERT INTO products(id, model_id, category, brand, aliases, msrp_new, fake_risk, version, state, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, 'active', ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                model_id = excluded.model_id,
                category = excluded.category,
                brand = excluded.brand,
                aliases = excluded.aliases,
                msrp_new = excluded.msrp_new,
                fake_risk = excluded.fake_risk,
                updated_at = excluded.updated_at
            """,
            (
                model_id,
                model_id,
                spec.get("category", ""),
                spec.get("brand"),
                aliases,
                spec.get("msrp_new"),
                spec.get("fake_risk"),
                now_str,
                now_str,
            ),
        )
    await conn.commit()


async def search_listings_fts_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    query: str,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Search listings using SQLite FTS5."""
    conn = await connect()
    safe_q = "".join(c for c in query if c.isalnum() or c in " *")
    if not safe_q.strip():
        return []
    cur = await conn.execute(
        """
        SELECT l.*, bm.rank
        FROM listings_fts(?) bm
        JOIN listings l ON l.avito_id = bm.avito_id
        ORDER BY bm.rank
        LIMIT ?
        """,
        (safe_q, limit),
    )
    return [dict(r) for r in await cur.fetchall()]


async def save_image_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    avito_id: str,
    url: str,
    local_path: str | None = None,
    phash: str | None = None,
) -> None:
    """Save image record and optional phash for a listing."""
    conn = await connect()
    await conn.execute(
        """
        INSERT INTO images(avito_id, url, local_path, phash)
        VALUES (?, ?, ?, ?)
        """,
        (avito_id, url, local_path, phash),
    )
    await conn.commit()


async def get_images_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    avito_id: str,
) -> list[dict[str, Any]]:
    """Fetch all images associated with a listing."""
    conn = await connect()
    cur = await conn.execute(
        "SELECT * FROM images WHERE avito_id = ? ORDER BY id ASC",
        (avito_id,),
    )
    return [dict(r) for r in await cur.fetchall()]


async def find_duplicate_images_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    phash: str,
    exclude_avito_id: str,
    max_distance: int = 4,
) -> list[dict[str, Any]]:
    """Find images with perceptual hash distance within threshold."""
    if not phash:
        return []
    conn = await connect()
    cur = await conn.execute(
        """
        SELECT i.avito_id, i.url, i.local_path, i.phash, l.title, l.seller_id
        FROM images i
        JOIN listings l ON l.avito_id = i.avito_id
        WHERE i.avito_id != ? AND i.phash IS NOT NULL
        """,
        (exclude_avito_id,),
    )
    rows = [dict(r) for r in await cur.fetchall()]
    import imagehash

    try:
        target_hash = imagehash.hex_to_hash(phash)
    except Exception:
        return []
    matches = []
    for r in rows:
        try:
            candidate_hash = imagehash.hex_to_hash(r["phash"])
            dist = target_hash - candidate_hash
            if dist <= max_distance:
                matches.append({**r, "distance": dist})
        except Exception:
            continue
    return matches


async def get_product_profile_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    model_id: str,
) -> dict[str, Any] | None:
    """Fetch cached product profile json by model_id."""
    conn = await connect()
    cur = await conn.execute(
        "SELECT profile_json FROM product_profiles WHERE model_id = ?",
        (model_id,),
    )
    row = await cur.fetchone()
    if not row:
        return None
    try:
        return json.loads(row["profile_json"])
    except Exception:
        return None


async def save_product_profile_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    model_id: str,
    category: str,
    canonical_name: str,
    brand: str,
    profile: dict[str, Any],
) -> None:
    """Insert or update a product profile cache record."""
    conn = await connect()
    now = utcnow()
    await conn.execute(
        """
        INSERT INTO product_profiles(
            model_id, category, canonical_name, brand, profile_json, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(model_id) DO UPDATE SET
            category = excluded.category,
            canonical_name = excluded.canonical_name,
            brand = excluded.brand,
            profile_json = excluded.profile_json,
            updated_at = excluded.updated_at
        """,
        (
            model_id,
            category,
            canonical_name,
            brand,
            json.dumps(profile, ensure_ascii=False),
            now,
        ),
    )
    await conn.commit()


async def set_pending_vision_db(
    set_runtime: Callable[[str, str], Awaitable[None]],
    avito_id: str,
    watchlist_id: str,
    attempts: int,
    next_try_at: str,
) -> None:
    """Record pending vision check state in runtime settings."""
    data = {
        "avito_id": avito_id,
        "watchlist_id": watchlist_id,
        "attempts": attempts,
        "next_try_at": next_try_at,
    }
    await set_runtime(f"pending_vision:{avito_id}", json.dumps(data))


async def get_pending_vision_db(
    get_runtime: Callable[[str, str], Awaitable[str]],
    avito_id: str,
) -> dict[str, Any] | None:
    """Fetch and parse pending vision state from runtime settings."""
    raw = await get_runtime(f"pending_vision:{avito_id}", "")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


async def enqueue_job_db(
    conn: aiosqlite.Connection,
    kind: str,
    product_id: str,
    dedupe_key: str,
    payload: dict[str, Any] | str,
    priority: int = 0,
    max_attempts: int = 3,
    next_run_at: str | None = None,
) -> int | None:
    """Enqueue a background job with deduplication and locking retries."""
    payload_str = json.dumps(payload, ensure_ascii=False) if isinstance(payload, dict) else str(payload)
    now_str = now_utc().isoformat()

    for attempt in range(3):
        try:
            cur = await conn.execute(
                "SELECT id FROM jobs WHERE kind = ? AND dedupe_key = ? AND status IN ('queued', 'leased')",
                (kind, dedupe_key),
            )
            row = await cur.fetchone()
            if row:
                return row["id"]

            cur = await conn.execute(
                """
                INSERT INTO jobs (kind, product_id, dedupe_key, payload, priority, status, attempts, max_attempts, next_run_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'queued', 0, ?, ?, ?, ?)
                """,
                (kind, product_id, dedupe_key, payload_str, priority, max_attempts, next_run_at, now_str, now_str),
            )
            await conn.commit()
            return cur.lastrowid
        except (sqlite3.IntegrityError, aiosqlite.IntegrityError):
            cur = await conn.execute(
                "SELECT id FROM jobs WHERE kind = ? AND dedupe_key = ? AND status IN ('queued', 'leased')",
                (kind, dedupe_key),
            )
            row = await cur.fetchone()
            return row["id"] if row else None
        except (sqlite3.OperationalError, aiosqlite.OperationalError) as db_err:
            try:
                await conn.rollback()
            except Exception:
                pass
            if "locked" in str(db_err).lower() and attempt < 2:
                await asyncio.sleep(0.05 * (2 ** attempt))
                continue
            raise


