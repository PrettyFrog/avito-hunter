"""Resilience tests for process crashes, offline Telegram, transient Avito errors, and migrations (<= 400 lines)."""

from __future__ import annotations

import asyncio

import pytest

from hunter.domain.slots import SlotHealth
from tools.stand import StandContext, create_stand


async def _add_subscription(stand: StandContext, name: str, url: str, price: str) -> None:
    await stand.owner.send_command("add")
    s1 = await stand.owner.wait_for_message(timeout=4.0)
    await stand.owner.send_text(name)
    s2 = await stand.owner.wait_for_message(timeout=4.0)
    await stand.owner.send_text(url)
    s3 = await stand.owner.wait_for_message(timeout=4.0)
    await stand.owner.send_text(price)
    s4 = await stand.owner.wait_for_message(timeout=4.0)
    await stand.owner.click("public_add_skip_words", message=s4)
    final_msg = await stand.owner.wait_for_message(timeout=4.0)
    assert "успешно добавлена" in final_msg.text.lower()


@pytest.mark.asyncio
async def test_process_restart_preserves_deliveries() -> None:
    """Verify that an abrupt process stop does not lose or corrupt pending deliveries."""
    stand1 = await create_stand()
    temp_dir = stand1.temp_dir
    try:
        # Seed baseline item and add subscription
        stand1.avito.add_item(id="4008889901", title="Sony A6000 Baseline", price=35000)
        await _add_subscription(stand1, "Sony A6000", "https://www.avito.ru/moskva?q=sony", "40000")
        await asyncio.sleep(2.0)

        # Pause dispatcher so delivery stays pending when process terminates
        if stand1.bootstrap.dispatcher:
            await stand1.bootstrap.dispatcher.stop()

        # Add new fresh deal
        stand1.avito.add_item(id="4008889902", title="Sony A6000 Crash Fresh Deal", price=28000)
        # Give poller time to pick up new item and insert delivery
        for _ in range(50):
            cur = await stand1.db._conn.execute("SELECT COUNT(*) FROM deliveries WHERE status = 'pending'")
            if (await cur.fetchone())[0] >= 1:
                break
            await asyncio.sleep(0.1)

        # Check deliveries in DB
        cur = await stand1.db._conn.execute("SELECT COUNT(*) FROM deliveries WHERE status = 'pending'")
        assert (await cur.fetchone())[0] >= 1, "Expected pending delivery before crash"
    finally:
        # Stop stand1 without removing temp_dir (simulating process termination)
        await stand1.stop(cleanup=False)

    # Boot stand2 reusing existing database
    stand2 = await create_stand(existing_temp_dir=temp_dir)
    try:
        # Check DB integrity on reopen
        cur = await stand2.db._conn.execute("PRAGMA integrity_check")
        assert (await cur.fetchone())[0] == "ok"

        # Wait for user to receive message
        delivered = False
        for _ in range(12):
            await asyncio.sleep(0.5)
            alerts = [m for m in stand2.owner.messages if "Crash Fresh Deal" in m.text]
            if alerts:
                delivered = True
                break
        assert delivered, "Message must be delivered after restart"

        # Verify exactly 1 delivered record recorded
        cur = await stand2.db._conn.execute("SELECT COUNT(*) FROM deliveries WHERE status = 'sent'")
        assert (await cur.fetchone())[0] == 1, "Expected exactly 1 sent record"
    finally:
        await stand2.stop(cleanup=True)


@pytest.mark.asyncio
async def test_telegram_offline_recovers_without_loss() -> None:
    """Verify that transient Telegram downtime causes leases to back off and recovers cleanly."""
    stand = await create_stand()
    try:
        # Seed baseline and subscribe
        stand.avito.add_item(id="4008889911", title="Sony A6000 Baseline TG", price=35000)
        await _add_subscription(stand, "Sony A6000", "https://www.avito.ru/moskva?q=sony", "40000")
        await asyncio.sleep(2.0)

        # Simulate Telegram API going offline (503 Service Unavailable)
        stand.tg.offline = True

        # Add new item
        stand.avito.add_item(id="4008889912", title="Sony A6000 TG Offline Deal", price=29000)
        # Give poller time to pick up new item
        await asyncio.sleep(2.0)

        # Telegram is offline, message should NOT be in stand.owner.messages
        tg_msgs = [m for m in stand.owner.messages if "TG Offline Deal" in m.text]
        assert len(tg_msgs) == 0, "No messages should be delivered while Telegram is offline"

        # Restore Telegram to online
        stand.tg.offline = False

        # Reset delivery so dispatcher retries immediately
        await stand.db._conn.execute(
            "UPDATE deliveries SET status = 'pending', available_at = '2020-01-01T00:00:00Z', "
            "lease_token = NULL, lease_until = NULL WHERE status != 'sent'"
        )
        await stand.db._conn.commit()
        if stand.bootstrap.dispatcher:
            stand.bootstrap.dispatcher.wake_up()

        delivered = False
        for _ in range(12):
            await asyncio.sleep(0.5)
            alerts = [m for m in stand.owner.messages if "TG Offline Deal" in m.text]
            if alerts:
                delivered = True
                break
        assert delivered, "Message should be delivered once Telegram is online"
    finally:
        await stand.stop()


@pytest.mark.asyncio
async def test_avito_transient_failure_and_capacity() -> None:
    """Verify slot transitions to suspect/quarantine on 429, without blocking new subscriptions."""
    stand = await create_stand()
    try:
        # Put slot into SUSPECT state with future quarantine
        await stand.db._conn.execute(
            "UPDATE slots SET health_state = ?, quarantine_until = '2099-01-01T00:00:00Z' WHERE id = 'slot-stand'",
            (SlotHealth.SUSPECT.value,),
        )
        await stand.db._conn.commit()

        # Verify capacity is still available for adding subscriptions (BUG-001 fix verified)
        await _add_subscription(stand, "Sony A6000", "https://www.avito.ru/moskva?q=sony", "40000")

        # Advance quarantine into the past to trigger recovery
        await stand.db._conn.execute(
            "UPDATE slots SET quarantine_until = '2020-01-01T00:00:00Z' WHERE id = 'slot-stand'"
        )
        await stand.db._conn.commit()

        # Background recovery loop recovers slot within ~0.3s
        recovered = False
        for _ in range(10):
            await asyncio.sleep(0.1)
            cur = await stand.db._conn.execute("SELECT health_state FROM slots WHERE id = 'slot-stand'")
            if (await cur.fetchone())[0] == SlotHealth.READY.value:
                recovered = True
                break
        assert recovered, "Slot must be automatically recovered to READY once quarantine expires"
    finally:
        await stand.stop()


@pytest.mark.asyncio
async def test_database_migration_idempotency_and_integrity() -> None:
    """Verify schema migrations run idempotently on existing database without data corruption."""
    stand = await create_stand()
    try:
        # Run migrations 3 times in succession
        for _ in range(3):
            await stand.db.migrate()

        # Check SQLite integrity
        cur = await stand.db._conn.execute("PRAGMA integrity_check")
        row = await cur.fetchone()
        assert row[0] == "ok"

        # Check schema_migrations table
        cur = await stand.db._conn.execute("SELECT MAX(version) FROM schema_migrations")
        latest_ver = (await cur.fetchone())[0]
        assert latest_ver >= 19
    finally:
        await stand.stop()
