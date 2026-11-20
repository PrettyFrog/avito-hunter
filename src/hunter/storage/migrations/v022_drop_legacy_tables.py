"""Drop 15 empty legacy v1 tables and clean up database schema (Stage 4.3)."""

from __future__ import annotations

import inspect
from typing import Any

V022_CHECKSUM_INPUT = "drop legacy tables: products, watchlist, notifications, evaluations, photo_checks, etc."

_LEGACY_TABLES = [
    "products",
    "product_profiles",
    "watchlist",
    "photo_cache",
    "photo_checks",
    "photo_shadow_results",
    "evaluations",
    "extractions",
    "market_snapshots",
    "notifications",
    "loose_triage_cache",
    "shadow_fetch",
    "memory_log",
    "feedback",
]

_PENDING_PRODUCTS_SQL = """
CREATE TABLE IF NOT EXISTS pending_products (
  chat_id INTEGER PRIMARY KEY,
  step TEXT NOT NULL,
  payload TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  owner_user_id INTEGER REFERENCES users(id)
)
"""


async def _run(conn: Any, sql: str) -> Any:
    res = conn.execute(sql)
    return await res if inspect.isawaitable(res) else res


async def apply_v022_migration(conn: Any, path: Any = None) -> None:
    await _run(conn, "BEGIN IMMEDIATE")
    try:
        for tbl in _LEGACY_TABLES:
            await _run(conn, f"DROP TABLE IF EXISTS {tbl}")
        await _run(conn, _PENDING_PRODUCTS_SQL)
        await _run(conn, "PRAGMA user_version = 22")
        res = conn.commit()
        if inspect.isawaitable(res):
            await res
    except BaseException:
        res = conn.rollback()
        if inspect.isawaitable(res):
            await res
        raise
