"""Database slice 52: Photo shadow evaluation results storage and retrieval."""

import json
from typing import Any
import aiosqlite

from hunter.core.models import now_utc


async def save_photo_shadow_result_db(
    conn: aiosqlite.Connection,
    product_id: str,
    avito_id: str,
    result: Any,
    photo_paths: list[Any] | None = None,
) -> None:
    """Persist photo shadow evaluation result row."""
    now_str = now_utc().isoformat()
    ev_json = json.dumps(getattr(result, "evidence", []), ensure_ascii=False)
    diff_json = json.dumps(getattr(result, "differences", []), ensure_ascii=False)
    det_json = json.dumps(getattr(result, "details", {}), ensure_ascii=False)
    paths_json = json.dumps([str(p) for p in (photo_paths or [])], ensure_ascii=False)
    await conn.execute(
        """
        INSERT INTO photo_shadow_results (
            product_id, avito_id, ts, status,
            signal_t, signal_o, signal_d, signal_c,
            evidence, differences, warning_text, skip_reason,
            details, photo_paths, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            product_id,
            avito_id,
            now_str,
            getattr(result, "status", "unverified"),
            getattr(result, "signal_t", "none"),
            getattr(result, "signal_o", "none"),
            getattr(result, "signal_d", "none"),
            getattr(result, "signal_c", "none"),
            ev_json,
            diff_json,
            getattr(result, "warning_text", ""),
            getattr(result, "skip_reason", ""),
            det_json,
            paths_json,
            now_str,
        ),
    )
    await conn.commit()


def parse_photo_shadow_row(r: Any) -> dict[str, Any]:
    """Parse single photo shadow result database row into dictionary."""
    return {
        "id": r[0],
        "product_id": r[1],
        "avito_id": r[2],
        "ts": r[3],
        "status": r[4],
        "signal_t": r[5],
        "signal_o": r[6],
        "signal_d": r[7],
        "signal_c": r[8],
        "evidence": json.loads(r[9]) if r[9] else [],
        "differences": json.loads(r[10]) if r[10] else [],
        "warning_text": r[11],
        "skip_reason": r[12],
        "details": json.loads(r[13]) if r[13] else {},
        "photo_paths": json.loads(r[14]) if r[14] else [],
        "created_at": r[15],
    }


async def get_photo_shadow_results_db(
    conn: aiosqlite.Connection,
    product_id: str,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Query recent photo shadow results for a product."""
    cur = await conn.execute(
        """
        SELECT id, product_id, avito_id, ts, status,
               signal_t, signal_o, signal_d, signal_c,
               evidence, differences, warning_text, skip_reason,
               details, photo_paths, created_at
        FROM photo_shadow_results
        WHERE product_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (product_id, limit),
    )
    rows = await cur.fetchall()
    return [parse_photo_shadow_row(r) for r in rows]


async def count_today_photo_shadow_results_db(conn: aiosqlite.Connection, product_id: str) -> int:
    """Count photo shadow results recorded today for a product."""
    cur = await conn.execute(
        """
        SELECT COUNT(*)
        FROM photo_shadow_results
        WHERE product_id = ? AND date(ts) = date('now')
        """,
        (product_id,),
    )
    row = await cur.fetchone()
    return row[0] if row else 0


async def record_shadow_fetch_db(conn: aiosqlite.Connection, record: dict[str, Any]) -> int:
    """Record shadow fetch telemetry into shadow_fetch table."""
    cur = await conn.execute(
        """
        INSERT INTO shadow_fetch (
            ts, watch_id, variant, status, final_url, latency_ms, bytes,
            n_items, n_with_price, n_extra, has_grid,
            id_overlap_pct, same_order_top20, price_equal_pct,
            block_marker, rss_chromium_mb, rss_node_mb
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record.get("ts") or now_utc().isoformat(),
            record["watch_id"],
            record["variant"],
            int(record.get("status", 0)),
            record.get("final_url", ""),
            int(record.get("latency_ms", 0)),
            int(record.get("bytes", 0)),
            int(record.get("n_items", 0)),
            int(record.get("n_with_price", 0)),
            int(record.get("n_extra", 0)),
            int(record.get("has_grid", 0)),
            record.get("id_overlap_pct"),
            record.get("same_order_top20"),
            record.get("price_equal_pct"),
            record.get("block_marker"),
            record.get("rss_chromium_mb"),
            record.get("rss_node_mb"),
        ),
    )
    await conn.commit()
    return cur.lastrowid


async def get_shadow_fetch_stats_db(
    conn: aiosqlite.Connection,
    watch_id: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Retrieve shadow fetch telemetry rows."""
    query = "SELECT * FROM shadow_fetch"
    params: list[Any] = []
    if watch_id:
        query += " WHERE watch_id = ?"
        params.append(watch_id)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    cur = await conn.execute(query, tuple(params))
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_shadow_today_count_db(
    conn: aiosqlite.Connection,
    watch_id: str | None = None,
) -> int:
    """Count shadow fetches performed today."""
    query = "SELECT COUNT(*) FROM shadow_fetch WHERE date(ts) = date('now')"
    params: list[Any] = []
    if watch_id:
        query += " AND watch_id = ?"
        params.append(watch_id)
    cur = await conn.execute(query, tuple(params))
    row = await cur.fetchone()
    return row[0] if row else 0


async def get_shadow_total_count_db(
    conn: aiosqlite.Connection,
    watch_id: str | None = None,
) -> int:
    """Count total shadow fetches."""
    query = "SELECT COUNT(*) FROM shadow_fetch"
    params: list[Any] = []
    if watch_id:
        query += " WHERE watch_id = ?"
        params.append(watch_id)
    cur = await conn.execute(query, tuple(params))
    row = await cur.fetchone()
    return row[0] if row else 0


async def get_shadow_last_ts_db(
    conn: aiosqlite.Connection,
    watch_id: str | None = None,
) -> str | None:
    """Get timestamp of most recent shadow fetch."""
    query = "SELECT ts FROM shadow_fetch"
    params: list[Any] = []
    if watch_id:
        query += " WHERE watch_id = ?"
        params.append(watch_id)
    query += " ORDER BY id DESC LIMIT 1"
    cur = await conn.execute(query, tuple(params))
    row = await cur.fetchone()
    return row[0] if row else None

