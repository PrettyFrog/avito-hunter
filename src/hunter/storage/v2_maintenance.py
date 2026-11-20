"""Maintenance and retention pruning for v2 storage entities (T-B015)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
import aiosqlite


async def cleanup_v2_records(
    conn: aiosqlite.Connection, now: Any, del_days: int, evt_days: int
) -> dict[str, int]:
    """Prunes completed outbox deliveries, recipient fanouts, and orphaned events."""
    del_cutoff = (now - timedelta(days=del_days)).isoformat()
    evt_cutoff = (now - timedelta(days=evt_days)).isoformat()

    cur = await conn.execute(
        "DELETE FROM deliveries WHERE status IN ('sent', 'expired', 'suppressed') AND created_at < ?",
        (del_cutoff,),
    )
    del_cnt = cur.rowcount

    cur = await conn.execute("DELETE FROM recipient_events WHERE created_at < ?", (evt_cutoff,))
    rec_cnt = cur.rowcount

    cur = await conn.execute(
        """
        DELETE FROM events
        WHERE created_at < ?
          AND id NOT IN (SELECT event_id FROM deliveries WHERE status IN ('pending', 'claimed'))
        """,
        (evt_cutoff,),
    )
    return {"deliveries": del_cnt, "recipient_events": rec_cnt, "events": cur.rowcount}
