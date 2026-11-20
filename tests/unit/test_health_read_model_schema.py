from datetime import UTC, datetime

import aiosqlite
import pytest

from hunter.storage.migrations.v012_subscriptions import V012_SQL
from hunter.storage.migrations.v013_events import V013_SQL
from hunter.storage.migrations.v014_slots import V014_SQL
from hunter.storage.read_models import fetch_system_health


@pytest.mark.asyncio
async def test_health_uses_real_schema_and_marks_unmeasured_lag_unknown():
    async with aiosqlite.connect(":memory:") as conn:
        await conn.executescript(V012_SQL + V013_SQL + V014_SQL)
        await conn.execute(
            "INSERT INTO queries (id, canonical_url, bootstrap_state) VALUES (?, ?, ?)",
            ("q1", "https://www.avito.ru/x", "active"),
        )
        await conn.execute(
            "INSERT INTO deliveries (id, event_id, user_id, channel, available_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("d1", "e1", 1, "telegram", "2026-10-23 11:50:00", "2026-10-23 11:50:00"),
        )
        health = await fetch_system_health(conn, datetime(2026, 10, 23, 12, tzinfo=UTC))
        assert health.active_queries == 1
        assert health.oldest_pending_delivery_age_sec == 600
        assert health.slot_schedule_lag_sec is None


@pytest.mark.asyncio
async def test_health_does_not_hide_missing_required_tables():
    async with aiosqlite.connect(":memory:") as conn:
        with pytest.raises(aiosqlite.OperationalError):
            await fetch_system_health(conn)
