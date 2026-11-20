from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import aiosqlite
from loguru import logger

from hunter.core.db import Database
from hunter.core.seed_parts.seed_from_legacy_01 import (
    read_legacy_blocklist,
    read_legacy_listings,
    read_legacy_sellers_and_notifs,
    transfer_listings,
    transfer_notifications,
    transfer_sellers,
)
from hunter.core.seed_parts.seed_from_legacy_03 import (
    transfer_blocklist,
    verify_new_db_integrity,
)

__all__ = ["calculate_sha256", "seed_from_legacy"]


def calculate_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


async def _read_all_legacy_data(
    old_conn: aiosqlite.Connection,
    old_path: Path,
) -> tuple[dict[str, int], list[str], list[Any], list[Any], list[Any], list[Any]] | None:
    cur = await old_conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    old_tables = {r[0] for r in await cur.fetchall()}

    res = await read_legacy_listings(old_conn, old_tables, old_path)
    if res is None:
        return None
    old_listing_cols, old_listings = res

    has_price_suspect = "price_suspect" in old_listing_cols
    old_sellers, old_notifs, feedback_count = await read_legacy_sellers_and_notifs(
        old_conn, old_tables, has_price_suspect
    )
    old_blocklist = await read_legacy_blocklist(old_conn, old_tables)

    stats = {
        "listings": len(old_listings),
        "sellers": len(old_sellers),
        "notifications": len(old_notifs),
        "feedback": feedback_count,
        "blocklist": len(old_blocklist),
    }
    return stats, old_listing_cols, old_listings, old_sellers, old_notifs, old_blocklist


async def _apply_seeded_data(
    new_path: Path,
    old_conn: aiosqlite.Connection,
    old_path: Path,
    stats: dict[str, int],
    old_listing_cols: list[str],
    old_listings: list[Any],
    old_sellers: list[Any],
    old_notifs: list[Any],
    old_blocklist: list[Any],
) -> None:
    new_db = Database(new_path)
    new_conn = await new_db.connect()
    try:
        await new_conn.execute("BEGIN IMMEDIATE")
        await transfer_listings(new_conn, old_listing_cols, old_listings)
        if old_sellers:
            await transfer_sellers(new_conn, old_conn, old_sellers)
        if old_notifs:
            await transfer_notifications(new_conn, old_conn, old_notifs)
        await transfer_blocklist(new_conn, old_conn, old_blocklist)
        await verify_new_db_integrity(new_conn)
        await new_conn.commit()
        logger.info("Successfully seeded fresh database from {}: {}", old_path, stats)
    except Exception as exc:
        try:
            await new_conn.execute("ROLLBACK")
        except Exception:
            pass
        logger.error("Seeding failed: {}", exc)
        raise
    finally:
        await new_db.close()


async def seed_from_legacy(
    old_path: Path,
    new_path: Path,
    *,
    dry_run: bool = False,
) -> dict[str, int]:
    """Migrate memory (real listings, sellers, notifications, blocklist)
    from legacy DB to fresh DB.

    Old DB is opened strictly read-only (mode=ro).
    New DB is initialized with Database(new_path) current schema.
    """
    if not old_path.exists():
        raise FileNotFoundError(f"Legacy database not found: {old_path}")

    sha_before = calculate_sha256(old_path)
    old_uri = f"file:{old_path.resolve()}?mode=ro"

    async with aiosqlite.connect(old_uri, uri=True) as old_conn:
        old_conn.row_factory = aiosqlite.Row

        legacy_data = await _read_all_legacy_data(old_conn, old_path)
        if legacy_data is None:
            return {"listings": 0, "sellers": 0, "notifications": 0, "feedback": 0, "blocklist": 0}
        stats, cols, listings, sellers, notifs, blocklist = legacy_data

        if dry_run:
            logger.info("[DRY-RUN] Legacy seed preview: {}", stats)
            sha_after = calculate_sha256(old_path)
            assert sha_before == sha_after, "Legacy database modified during dry-run!"
            return stats

        await _apply_seeded_data(
            new_path, old_conn, old_path, stats, cols, listings, sellers, notifs, blocklist
        )

    sha_after = calculate_sha256(old_path)
    assert sha_before == sha_after, (
        f"Legacy DB modified during seeding! ({sha_before} != {sha_after})"
    )

    return stats
