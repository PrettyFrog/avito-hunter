"""Normalize legacy products table shape without discarding catalog data."""

from __future__ import annotations

import inspect
from typing import Any

V019_CHECKSUM_INPUT = "products.id-primary-key; canonical nullability; preserve catalog rows"
V019_CREATE_PRODUCTS_SQL = """
CREATE TABLE products_v19 (
    id TEXT PRIMARY KEY,
    version INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT,
    model_id TEXT,
    category TEXT,
    brand TEXT,
    aliases TEXT,
    msrp_new INTEGER,
    fake_risk TEXT,
    owner_user_id INTEGER REFERENCES users(id)
)
"""
EXPECTED_COLUMNS = frozenset(
    {
        "id", "version", "state", "created_at", "updated_at", "model_id",
        "category", "brand", "aliases", "msrp_new", "fake_risk", "owner_user_id",
    }
)


async def _call(conn: Any, sql: str, params: tuple[Any, ...] = ()) -> Any:
    result = conn.execute(sql, params)
    return await result if inspect.isawaitable(result) else result


async def _fetchall(conn: Any, sql: str) -> list[Any]:
    cursor = await _call(conn, sql)
    result = cursor.fetchall()
    return await result if inspect.isawaitable(result) else result


def _row_values(row: Any, columns: list[str]) -> tuple[Any, ...]:
    values = dict(row) if hasattr(row, "keys") else dict(zip(columns, row, strict=False))
    product_id = values.get("id") or values.get("model_id")
    if not product_id:
        raise ValueError("Cannot migrate products row without id or model_id")
    created_at = values.get("created_at")
    return (
        product_id,
        values.get("version") or 1,
        values.get("state") or "active",
        created_at or "",
        values.get("updated_at"),
        values.get("model_id"),
        values.get("category"),
        values.get("brand"),
        values.get("aliases"),
        values.get("msrp_new"),
        values.get("fake_risk"),
        values.get("owner_user_id"),
    )


async def _rebuild_products(conn: Any) -> None:
    info = await _fetchall(conn, "PRAGMA table_info(products)")
    columns = [str(row[1]) for row in info]
    if not columns or set(columns) - EXPECTED_COLUMNS:
        raise RuntimeError("Unexpected products columns; refusing lossy schema normalization")
    rows = await _fetchall(conn, "SELECT * FROM products")
    values = [_row_values(row, columns) for row in rows]
    await _call(conn, V019_CREATE_PRODUCTS_SQL)
    for product in values:
        await _call(
            conn,
            "INSERT INTO products_v19 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            product,
        )
    await _call(conn, "DROP TABLE products")
    await _call(conn, "ALTER TABLE products_v19 RENAME TO products")
    await _call(conn, "CREATE UNIQUE INDEX idx_products_model_id ON products(model_id)")


async def apply_v019_migration(conn: Any, path: Any = None) -> None:
    """Rebuild pre-v11 products tables to the canonical `id` primary key shape."""
    await _call(conn, "BEGIN IMMEDIATE")
    try:
        info = await _fetchall(conn, "PRAGMA table_info(products)")
        if not info:
            await _call(conn, "PRAGMA user_version = 19")
            res = conn.commit()
            if inspect.isawaitable(res):
                await res
            return
        if "owner_user_id" not in {str(row[1]) for row in info}:
            await _call(conn, "ALTER TABLE products ADD COLUMN owner_user_id INTEGER REFERENCES users(id)")
            info = await _fetchall(conn, "PRAGMA table_info(products)")
        id_pk = any(str(row[1]) == "id" and int(row[5]) == 1 for row in info)
        canonical = {str(row[1]): row for row in info}
        if not id_pk or any(
            name not in canonical or int(canonical[name][3]) != 1
            for name in ("version", "state", "created_at")
        ):
            await _rebuild_products(conn)
        await _call(conn, "CREATE UNIQUE INDEX IF NOT EXISTS idx_products_id ON products(id)")
        await _call(conn, "CREATE UNIQUE INDEX IF NOT EXISTS idx_products_model_id ON products(model_id)")
        await _call(conn, "PRAGMA user_version = 19")
        result = conn.commit()
        if inspect.isawaitable(result):
            await result
    except Exception:
        result = conn.rollback()
        if inspect.isawaitable(result):
            await result
        raise
