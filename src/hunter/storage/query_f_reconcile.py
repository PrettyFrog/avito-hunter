"""Self-healing migration for queries with conflicting or desynchronized f-parameter price bounds."""
from __future__ import annotations

import logging
from typing import Any
from urllib.parse import parse_qs, urlparse

from hunter.application.subscription_rebind import change_query_price
from hunter.application.subscriptions import compute_query_id
from hunter.domain.query_url import extract_f_price_ceiling

logger = logging.getLogger(__name__)


async def _reconcile_single_query(
    conn: Any, sub_id: str, old_qid: str, max_price: int, canonical_url: str
) -> tuple[str, str] | None:
    parsed = urlparse(canonical_url)
    qs = parse_qs(parsed.query, keep_blank_values=False)
    f_vals = qs.get("f")
    if not f_vals:
        return None
    ceiling = extract_f_price_ceiling(f_vals[0])
    if ceiling is None or ceiling == max_price:
        return None

    new_url = change_query_price(canonical_url, max_price)
    new_qid = compute_query_id(new_url)
    if new_qid == old_qid:
        return None

    logger.info(
        "Self-healing conflicting query for sub {}: {} -> {} (f ceiling {} -> {})",
        sub_id, old_qid, new_qid, ceiling, max_price,
    )
    await conn.execute(
        "INSERT OR IGNORE INTO queries (id, canonical_url, bootstrap_state) VALUES (?, ?, 'ready')",
        (new_qid, new_url),
    )
    await conn.execute(
        "UPDATE subscriptions SET query_id = ?, updated_at = datetime('now') WHERE id = ?",
        (new_qid, sub_id),
    )
    cur = await conn.execute("SELECT id FROM assignments WHERE query_id = ? AND active = 1", (new_qid,))
    if await cur.fetchone():
        await conn.execute("UPDATE assignments SET active = 0, updated_at = datetime('now') WHERE query_id = ?", (old_qid,))
    else:
        await conn.execute("UPDATE assignments SET query_id = ?, updated_at = datetime('now') WHERE query_id = ?", (new_qid, old_qid))

    await conn.execute(
        "INSERT OR IGNORE INTO query_membership (query_id, avito_id, first_seen, last_seen, baseline_epoch) "
        "SELECT ?, avito_id, first_seen, last_seen, baseline_epoch FROM query_membership WHERE query_id = ?",
        (new_qid, old_qid),
    )
    await conn.execute(
        "DELETE FROM queries WHERE id = ? AND NOT EXISTS (SELECT 1 FROM subscriptions WHERE query_id = ?)",
        (old_qid, old_qid),
    )
    return (old_qid, new_qid)


async def reconcile_conflicting_f_queries(writer: Any) -> list[tuple[str, str]]:
    """Reconciles queries where the 'f' parameter price ceiling conflicts with max_price."""
    async def _reconcile_tx(conn: Any) -> list[tuple[str, str]]:
        cur = await conn.execute(
            "SELECT s.id, s.query_id, s.max_price, q.canonical_url "
            "FROM subscriptions s "
            "JOIN queries q ON q.id = s.query_id"
        )
        rows = await cur.fetchall()
        reconciled = []
        for sub_id, old_qid, max_price, canonical_url in rows:
            rec = await _reconcile_single_query(conn, sub_id, old_qid, int(max_price), canonical_url)
            if rec:
                reconciled.append(rec)
        return reconciled

    return await writer.submit(_reconcile_tx)
