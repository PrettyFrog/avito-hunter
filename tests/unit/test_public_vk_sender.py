from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from hunter.application.dispatcher import SendResult
from hunter.notify.public_vk_sender import PublicVkSender
from hunter.notify.vk_delivery import VkDeliveryResult
from hunter.storage.connection import StorageConfig, StorageConnectionManager
from hunter.storage.deliveries import ClaimedDelivery
from hunter.storage.writer import SQLiteWriterActor


@pytest.fixture
async def vk_sender(tmp_path):
    db_path = tmp_path / "sender.db"
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            "CREATE TABLE channel_destinations "
            "(user_id INTEGER, channel TEXT, address TEXT, verified INTEGER, version INTEGER)"
        )
        await conn.execute("INSERT INTO channel_destinations VALUES (1, 'vk', '1234', 1, 3)")
        await conn.commit()
    manager = StorageConnectionManager(StorageConfig(db_path))
    adapter = SimpleNamespace(send_delivery=AsyncMock(return_value=SendResult("sent", "55")))
    yield PublicVkSender(manager, adapter), adapter, db_path
    await manager.drain_and_close()


def delivery(version=3, user_id=1, channel="vk"):
    return ClaimedDelivery("id", "event", user_id, channel, 1, {}, 1, "lease", "", version)


@pytest.mark.asyncio
async def test_sender_uses_verified_tenant_destination(vk_sender):
    sender, adapter, _ = vk_sender
    item = delivery()
    assert (await sender(item)).status == "sent"
    adapter.send_delivery.assert_awaited_once_with(item, 1234)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "item", [delivery(version=2), delivery(user_id=2), delivery(channel="telegram")]
)
async def test_sender_rejects_stale_foreign_and_wrong_channel(vk_sender, item):
    sender, adapter, _ = vk_sender
    assert (await sender(item)).status == "dead_letter"
    adapter.send_delivery.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("address,verified", [("0", 1), ("abc", 1), ("1234", 0)])
async def test_sender_rejects_invalid_or_revoked_destination(vk_sender, address, verified):
    sender, adapter, path = vk_sender
    async with aiosqlite.connect(path) as conn:
        await conn.execute(
            "UPDATE channel_destinations SET address = ?, verified = ?", (address, verified)
        )
        await conn.commit()
    assert (await sender(delivery())).status == "dead_letter"
    adapter.send_delivery.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("rebind", [False, True])
async def test_vk_block_revokes_only_the_version_that_failed(vk_sender, rebind):
    sender, adapter, path = vk_sender
    writer = SQLiteWriterActor(sender.connections)
    await writer.start()
    sender.writer = writer

    async def blocked(_delivery, _destination):
        if rebind:

            async def update(conn):
                await conn.execute(
                    "UPDATE channel_destinations SET address='4567', version=4 WHERE user_id=1"
                )

            await writer.submit(update)
        return VkDeliveryResult("blocked").to_send_result()

    adapter.send_delivery.side_effect = blocked
    try:
        assert (await sender(delivery())).status == "dead_letter"
        async with aiosqlite.connect(path) as conn:
            row = await (
                await conn.execute(
                    "SELECT address, verified, version FROM channel_destinations WHERE user_id=1"
                )
            ).fetchone()
        assert row == (("4567", 1, 4) if rebind else ("1234", 0, 3))
    finally:
        await writer.stop()


@pytest.mark.asyncio
async def test_vk_block_does_not_ack_when_revocation_writer_fails(vk_sender):
    sender, adapter, _ = vk_sender
    sender.writer = SimpleNamespace(submit=AsyncMock(side_effect=OSError("injected fault")))
    adapter.send_delivery.return_value = VkDeliveryResult("blocked").to_send_result()
    with pytest.raises(OSError, match="injected fault"):
        await sender(delivery())
