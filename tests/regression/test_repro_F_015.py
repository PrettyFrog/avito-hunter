"""Regression test for F-015: Retention cleanup of v2 tables.

Verifies that cleanup_old_records prunes historical completed/terminal deliveries
('sent', 'expired', 'suppressed') and unreferenced events older than retention cutoff,
while preserving active deliveries ('pending', 'claimed') and recent records.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest

from hunter.core.db import Database


@pytest.mark.asyncio
async def test_retention_cleans_v2_deliveries_and_events(tmp_path: Path) -> None:
    db = Database(tmp_path / "test_retention.db")
    await db.connect()

    conn = await db.connect()
    now = datetime.now(timezone.utc)
    old_ts = (now - timedelta(days=25)).isoformat()
    recent_ts = (now - timedelta(days=2)).isoformat()

    # 1. Seed events
    # event_old_terminal: referenced only by terminal delivery -> should be deleted
    # event_old_active: referenced by pending delivery -> should be preserved
    # event_recent: recent -> should be preserved
    await conn.execute(
        "INSERT INTO events (id, avito_id, revision, kind, observed_at, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("evt_old_term", "avito_1", 1, "NEW_LISTING", old_ts, old_ts),
    )
    await conn.execute(
        "INSERT INTO events (id, avito_id, revision, kind, observed_at, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("evt_old_active", "avito_2", 1, "NEW_LISTING", old_ts, old_ts),
    )
    await conn.execute(
        "INSERT INTO events (id, avito_id, revision, kind, observed_at, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("evt_recent", "avito_3", 1, "NEW_LISTING", recent_ts, recent_ts),
    )

    # 2. Seed recipient_events
    await conn.execute(
        "INSERT INTO recipient_events (user_id, event_id, created_at) VALUES (?, ?, ?)",
        (1001, "evt_old_term", old_ts),
    )
    await conn.execute(
        "INSERT INTO recipient_events (user_id, event_id, created_at) VALUES (?, ?, ?)",
        (1001, "evt_recent", recent_ts),
    )

    # 3. Seed deliveries
    # Old terminal deliveries -> should be deleted
    await conn.execute(
        "INSERT INTO deliveries (id, event_id, user_id, channel, status, available_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("del_old_sent", "evt_old_term", 1001, "telegram", "sent", old_ts, old_ts),
    )
    await conn.execute(
        "INSERT INTO deliveries (id, event_id, user_id, channel, status, available_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("del_old_expired", "evt_old_term", 1002, "telegram", "expired", old_ts, old_ts),
    )
    await conn.execute(
        "INSERT INTO deliveries (id, event_id, user_id, channel, status, available_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("del_old_suppressed", "evt_old_term", 1003, "telegram", "suppressed", old_ts, old_ts),
    )
    # Old pending delivery -> must NOT be deleted
    await conn.execute(
        "INSERT INTO deliveries (id, event_id, user_id, channel, status, available_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("del_old_pending", "evt_old_active", 1001, "telegram", "pending", old_ts, old_ts),
    )
    # Recent sent delivery -> must NOT be deleted
    await conn.execute(
        "INSERT INTO deliveries (id, event_id, user_id, channel, status, available_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("del_recent_sent", "evt_recent", 1001, "telegram", "sent", recent_ts, recent_ts),
    )
    await conn.commit()

    # 4. Run cleanup
    stats = await db.cleanup_old_records(
        deliveries_retention_days=14,
        events_retention_days=14,
    )

    # 5. Assertions
    assert stats.get("deliveries", 0) == 3, f"Expected 3 pruned deliveries, got {stats.get('deliveries')}"
    assert stats.get("recipient_events", 0) == 1, f"Expected 1 pruned recipient_event, got {stats.get('recipient_events')}"
    assert stats.get("events", 0) == 1, f"Expected 1 pruned event, got {stats.get('events')}"

    # Verify preserved records
    cur = await conn.execute("SELECT id FROM deliveries ORDER BY id")
    remaining_del = [r[0] for r in await cur.fetchall()]
    assert remaining_del == ["del_old_pending", "del_recent_sent"]

    cur = await conn.execute("SELECT id FROM events ORDER BY id")
    remaining_evt = [r[0] for r in await cur.fetchall()]
    assert remaining_evt == ["evt_old_active", "evt_recent"]

    await db.close()


@pytest.mark.asyncio
async def test_bootstrap_run_maintenance_invokes_cleanup(tmp_path: Path) -> None:
    from hunter.bootstrap import Bootstrap
    from hunter.core.config import Settings

    db = Database(tmp_path / "test_bootstrap_retention.db")
    await db.connect()

    settings = Settings()
    bootstrap = Bootstrap(settings, db=db)
    res = await bootstrap.run_maintenance()
    assert isinstance(res, dict)
    assert "deliveries" in res
    assert "events" in res

    await db.close()
