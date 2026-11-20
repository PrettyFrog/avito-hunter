"""Seed slice 01: Read legacy database entities (T-230)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import aiosqlite
from loguru import logger

from hunter.core.models import now_utc


async def read_legacy_listings(
    old_conn: aiosqlite.Connection,
    old_tables: set[str],
    old_path: Path,
) -> tuple[set[str], list[Any]] | None:
    """Read listings from legacy database and return columns and rows."""
    if "listings" not in old_tables:
        logger.warning("No listings table found in legacy DB {}", old_path)
        return None

    cur = await old_conn.execute("PRAGMA table_info(listings)")
    old_listing_cols = {r[1] for r in await cur.fetchall()}
    has_price_suspect = "price_suspect" in old_listing_cols

    query_listings = (
        'SELECT * FROM listings WHERE (price IS NOT NULL OR (description IS NOT '
            "NULL AND description != ''))"
        " AND (price IS NULL OR price <= 1000000)"
    )
    if has_price_suspect:
        query_listings += " AND (price_suspect IS NULL OR price_suspect = 0)"

    cur = await old_conn.execute(query_listings)
    old_listings = await cur.fetchall()
    return old_listing_cols, old_listings


async def read_legacy_sellers_and_notifs(
    old_conn: aiosqlite.Connection,
    old_tables: set[str],
    has_price_suspect: bool,
) -> tuple[list[Any], list[Any], int]:
    """Read sellers and notifications from legacy database."""
    if "sellers" in old_tables:
        query_sellers = """
        SELECT * FROM sellers 
        WHERE seller_id IN (
            SELECT DISTINCT seller_id FROM listings 
            WHERE (price IS NOT NULL OR (description IS NOT NULL AND description != ''))
            AND (price IS NULL OR price <= 1000000)
        """
        if has_price_suspect:
            query_sellers += " AND (price_suspect IS NULL OR price_suspect = 0)"
        query_sellers += " AND seller_id IS NOT NULL)"
        cur = await old_conn.execute(query_sellers)
        old_sellers = await cur.fetchall()
    else:
        old_sellers = []

    if "notifications" in old_tables:
        cur = await old_conn.execute("SELECT * FROM notifications")
        old_notifs = await cur.fetchall()
        feedback_count = sum(1 for n in old_notifs if dict(n).get("feedback") is not None)
    else:
        old_notifs = []
        feedback_count = 0

    return old_sellers, old_notifs, feedback_count


async def read_legacy_blocklist(
    old_conn: aiosqlite.Connection,
    old_tables: set[str],
) -> list[Any]:
    """Read blocklist from legacy database."""
    if "blocklist" in old_tables:
        cur = await old_conn.execute("SELECT * FROM blocklist")
        return await cur.fetchall()
    return []


async def transfer_listings(
    new_conn: aiosqlite.Connection,
    old_listing_cols: set[str],
    old_listings: list[Any],
) -> None:
    """Transfer legacy listings to new database schema."""
    cur = await new_conn.execute("PRAGMA table_info(listings)")
    new_listing_cols = [r[1] for r in await cur.fetchall()]
    common_listing_cols = [c for c in new_listing_cols if c in old_listing_cols]

    cols_insert = list(common_listing_cols)
    if "price_suspect" not in cols_insert and "price_suspect" in new_listing_cols:
        cols_insert.append("price_suspect")

    ph = ", ".join("?" for _ in cols_insert)
    sql_insert_listing = (
        f"INSERT OR IGNORE INTO listings ({', '.join(cols_insert)}) VALUES ({ph})"
    )

    listings_to_insert = []
    for row in old_listings:
        row_dict = dict(row)
        vals = []
        for c in cols_insert:
            if c == "price_suspect" and "price_suspect" not in row_dict:
                vals.append(0)
            else:
                vals.append(row_dict.get(c))
        listings_to_insert.append(vals)

    if listings_to_insert:
        await new_conn.executemany(sql_insert_listing, listings_to_insert)


async def transfer_sellers(
    new_conn: aiosqlite.Connection,
    old_conn: aiosqlite.Connection,
    old_sellers: list[Any],
) -> None:
    """Transfer legacy sellers to new database schema."""
    cur = await new_conn.execute("PRAGMA table_info(sellers)")
    new_seller_cols = [r[1] for r in await cur.fetchall()]
    cur = await old_conn.execute("PRAGMA table_info(sellers)")
    old_seller_cols = {r[1] for r in await cur.fetchall()}
    common_seller_cols = [c for c in new_seller_cols if c in old_seller_cols]
    ph_sellers = ", ".join("?" for _ in common_seller_cols)
    sql_insert_seller = (
        f"INSERT OR IGNORE INTO sellers ({', '.join(common_seller_cols)}) VALUES ({ph_sellers})"
    )
    sellers_to_insert = [
        [dict(r).get(c) for c in common_seller_cols] for r in old_sellers
    ]
    await new_conn.executemany(sql_insert_seller, sellers_to_insert)


async def transfer_notifications(
    new_conn: aiosqlite.Connection,
    old_conn: aiosqlite.Connection,
    old_notifs: list[Any],
) -> None:
    """Transfer legacy notifications to new database schema."""
    cur = await new_conn.execute("PRAGMA table_info(notifications)")
    new_notif_cols = [r[1] for r in await cur.fetchall()]
    cur = await old_conn.execute("PRAGMA table_info(notifications)")
    old_notif_cols = {r[1] for r in await cur.fetchall()}

    insert_notif_cols = [c for c in new_notif_cols if c in old_notif_cols]
    for extra_col in ("status", "attempts", "created_at"):
        if extra_col not in insert_notif_cols and extra_col in new_notif_cols:
            insert_notif_cols.append(extra_col)

    ph_notifs = ", ".join("?" for _ in insert_notif_cols)
    sql_insert_notif = (
        f"INSERT OR IGNORE INTO notifications ({', '.join(insert_notif_cols)}) VALUES ({ph_notifs})"
    )

    now_iso = now_utc().isoformat()
    notifs_to_insert = []
    for r in old_notifs:
        r_dict = dict(r)
        vals = []
        for c in insert_notif_cols:
            if c == "status" and "status" not in r_dict:
                vals.append("sent")
            elif c == "attempts" and "attempts" not in r_dict:
                vals.append(1)
            elif c == "created_at" and "created_at" not in r_dict:
                vals.append(r_dict.get("sent_at") or now_iso)
            else:
                vals.append(r_dict.get(c))
        notifs_to_insert.append(vals)

    await new_conn.executemany(sql_insert_notif, notifs_to_insert)
