"""Read local runtime health evidence without probing external services."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from hunter.system.doctor import RuntimeHealthSignals


async def read_runtime_health(db: Any) -> RuntimeHealthSignals:
    uri = Path(db.path).resolve().as_uri() + "?mode=ro"
    async with aiosqlite.connect(uri, uri=True) as conn:
        await conn.execute("PRAGMA query_only = ON")
        await conn.execute("PRAGMA busy_timeout = 15000")
        cursor = await conn.execute(
            "SELECT count(*), min(created_at) FROM deliveries WHERE status IN ('pending', 'leased')"
        )
        count, oldest = await cursor.fetchone()
        cursor = await conn.execute(
            "SELECT count(*) FROM slots WHERE lower(health_state) IN "
            "('quarantined', 'auth_required', 'challenge')"
        )
        quarantined = int((await cursor.fetchone())[0])
    age = 0.0
    if oldest:
        stamp = datetime.fromisoformat(str(oldest).replace("Z", "+00:00"))
        stamp = stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)
        age = max(0.0, (datetime.now(UTC) - stamp).total_seconds())
    wal = Path(str(db.path) + "-wal")
    try:
        wal_size = wal.stat().st_size
    except FileNotFoundError:
        wal_size = 0
    return RuntimeHealthSignals(
        pending_outbox_count=int(count), oldest_pending_age_sec=age,
        quarantined_slots=quarantined, wal_bytes=wal_size,
    )
