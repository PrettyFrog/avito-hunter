from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from tests.unit.test_delivery_repository import _insert_del

from hunter.application.dispatcher import (
    TELEGRAM_GLOBAL_COOLDOWN_KEY,
    DeliveryDispatcher,
    SendResult,
)
from hunter.notify.public_vk_sender import PublicVkSender
from hunter.notify.vk_delivery import VkDeliveryAdapter

pytest_plugins = ("tests.unit.test_delivery_repository",)


@pytest.mark.asyncio
async def test_failed_telegram_creates_and_delivers_verified_vk_intent(env):
    repo, writer, mgr, _ = env
    now = datetime.now(UTC).isoformat()
    await _insert_del(writer, "tg", now)

    async def seed(conn):
        await conn.execute(
            "INSERT INTO users (id, created_at, last_seen_at) VALUES (101, 't', 't')"
        )
        await conn.execute(
            "INSERT INTO channel_destinations (user_id, channel, address, verified, version) "
            "VALUES (101, 'vk', '1234', 1, 2)"
        )
        await conn.execute("UPDATE deliveries SET attempt=2 WHERE id='tg'")

    await writer.submit(seed)
    telegram = AsyncMock(return_value=SendResult("ambiguous"))
    requests = []

    async def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"response": 55}, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        vk = PublicVkSender(mgr, VkDeliveryAdapter("fake", client=client))
        dispatcher = DeliveryDispatcher(repo, {"telegram": telegram, "vk": vk})
        dispatcher._global_next_send = 0
        await dispatcher.dispatch_once(datetime.now(UTC).isoformat())
        reader = await mgr.open_read_connection()
        try:
            rows = await (
                await reader.execute(
                    "SELECT channel, status, destination_version FROM deliveries ORDER BY channel"
                )
            ).fetchall()
            assert [tuple(row) for row in rows] == [
                ("telegram", "pending", 1),
                ("vk", "pending", 2),
            ]
        finally:
            await mgr.close_reader(reader)
        await dispatcher.dispatch_once(datetime.now(UTC).isoformat())
    assert len(requests) == 1
    assert b"user_id=1234" in requests[0].content


@pytest.mark.asyncio
async def test_telegram_global_cooldown_does_not_block_vk(env):
    repo, writer, mgr, _ = env
    now = datetime.now(UTC)
    await _insert_del(writer, "vk", now.isoformat())
    await _insert_del(writer, "telegram", now.isoformat())

    async def switch_channel(conn):
        await conn.execute("UPDATE deliveries SET channel = 'vk' WHERE id = 'vk'")

    await writer.submit(switch_channel)
    await repo.extend_cooldown(TELEGRAM_GLOBAL_COOLDOWN_KEY, now + timedelta(minutes=5))
    vk = AsyncMock(return_value=SendResult("sent", "55"))
    dispatcher = DeliveryDispatcher(repo, {"vk": vk})
    assert await dispatcher.dispatch_once(now.isoformat()) == 1
    assert vk.await_count == 1
    reader = await mgr.open_read_connection()
    try:
        row = await (
            await reader.execute("SELECT status FROM deliveries WHERE id = 'vk'")
        ).fetchone()
        assert row[0] == "sent"
        row = await (
            await reader.execute("SELECT status, attempt FROM deliveries WHERE id = 'telegram'")
        ).fetchone()
        assert tuple(row) == ("pending", 0)
    finally:
        await mgr.close_reader(reader)


@pytest.mark.asyncio
async def test_temporarily_disabled_vk_keeps_pending_intent(env):
    repo, writer, mgr, _ = env
    now = datetime.now(UTC)
    await _insert_del(writer, "vk_disabled", now.isoformat())

    async def switch_channel(conn):
        await conn.execute("UPDATE deliveries SET channel = 'vk' WHERE id = 'vk_disabled'")

    await writer.submit(switch_channel)
    dispatcher = DeliveryDispatcher(repo, {})
    await dispatcher.dispatch_once(now.isoformat())
    reader = await mgr.open_read_connection()
    try:
        row = await (
            await reader.execute(
                "SELECT status, available_at FROM deliveries WHERE id = 'vk_disabled'"
            )
        ).fetchone()
        assert row[0] == "pending"
        assert datetime.fromisoformat(row[1]) > now
    finally:
        await mgr.close_reader(reader)

    async def make_due(conn):
        await conn.execute(
            "UPDATE deliveries SET available_at = ? WHERE id = 'vk_disabled'", (now.isoformat(),)
        )

    await writer.submit(make_due)
    vk = AsyncMock(return_value=SendResult("sent", "55"))
    await DeliveryDispatcher(repo, {"vk": vk}).dispatch_once(now.isoformat())
    assert vk.await_count == 1
