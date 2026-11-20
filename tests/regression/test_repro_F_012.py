"""Regression test for F-012: VK fallback leaves original Telegram delivery active, causing duplicate sending."""

from __future__ import annotations

from pathlib import Path
import aiosqlite
import pytest

from hunter.storage.connection import StorageConfig, StorageConnectionManager
from hunter.storage.deliveries import ClaimedDelivery, DeliveryRepository
from hunter.storage.writer import SQLiteWriterActor


@pytest.mark.asyncio
async def test_repro_F_012_vk_fallback_duplicates_telegram(clean_db_path: Path) -> None:
    schema_path = Path(__file__).resolve().parents[2] / "src/hunter/core/schema.sql"
    with open(schema_path, "r", encoding="utf-8") as sf:
        schema_sql = sf.read()

    from datetime import UTC, datetime, timedelta
    t0 = datetime.now(UTC)

    async with aiosqlite.connect(clean_db_path) as c:
        await c.executescript(schema_sql)
        await c.execute("INSERT INTO users (id, status, created_at) VALUES (100, 'active', ?)", (t0.isoformat(),))
        await c.execute("INSERT INTO channel_destinations (user_id, channel, address, verified, version) VALUES (100, 'vk', '12345678', 1, 1)")
        await c.execute("INSERT INTO events (id, avito_id, revision, kind, observed_at, new_price) VALUES ('ev1', 'av1', 1, 'NEW_LISTING', ?, 50000)", (t0.isoformat(),))
        await c.execute("""
            INSERT INTO deliveries (id, event_id, user_id, channel, destination_version, payload_version, payload, status, available_at, attempt)
            VALUES ('del_ev1_100_telegram', 'ev1', 100, 'telegram', 1, 1, '{"title": "Test Item"}', 'pending', ?, 0)
        """, (t0.isoformat(),))
        await c.commit()

    mgr = StorageConnectionManager(StorageConfig(db_path=clean_db_path))
    writer = SQLiteWriterActor(mgr)
    await writer.start()
    try:
        repo = DeliveryRepository(mgr, writer)
        claimed = await repo.claim_due((t0 + timedelta(seconds=1)).isoformat(), limit=10)
        assert len(claimed) == 1
        d = claimed[0]

        d_attempt3 = ClaimedDelivery(
            id=d.id, event_id=d.event_id, user_id=d.user_id, channel=d.channel,
            payload_version=d.payload_version, payload=d.payload, attempt=3,
            lease_token=d.lease_token, lease_until=d.lease_until, destination_version=d.destination_version
        )
        fallback_ok = await repo.retry_with_vk_fallback(d_attempt3, retry_after_seconds=5)
        assert fallback_ok is True

        vk_claimed = await repo.claim_due((datetime.now(UTC) + timedelta(seconds=1)).isoformat(), limit=10)
        vk_d = [item for item in vk_claimed if item.channel == "vk"][0]
        await repo.complete(vk_d.id, vk_d.lease_token, message_id="vk_msg_1")

        # After VK is successfully delivered, Telegram delivery must NOT be claimed again (F-012)
        tg_claimed = await repo.claim_due((datetime.now(UTC) + timedelta(seconds=10)).isoformat(), limit=10)
        tg_active = [item for item in tg_claimed if item.channel == "telegram"]
        assert len(tg_active) == 0, f"Telegram delivery re-claimed after VK fallback succeeded: {tg_active} (F-012)"
    finally:
        await writer.stop()
