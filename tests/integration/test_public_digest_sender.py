from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import pytest

from hunter.notify.digest_delivery import format_digest_chunks
from hunter.notify.public_telegram_sender import PublicTelegramSender
from hunter.storage.deliveries import ClaimedDelivery

pytest_plugins = ("tests.integration.test_public_hunter",)


def delivery(number, user=10, version=1):
    return ClaimedDelivery(
        f"d{number}",
        f"e{number}",
        user,
        "telegram",
        1,
        {"title": "Item", "new_price": 100, "url": f"https://avito.ru/item/{number}"},
        1,
        "lease",
        "",
        version,
    )


@pytest.mark.asyncio
async def test_digest_sender_sends_one_message_to_verified_recipient(public_v2_system):
    app, _, _, _ = public_v2_system
    await app.dispatcher.stop()
    bot = AsyncMock()
    bot.send_message.return_value = MagicMock(message_id=44)
    channel = MagicMock()
    channel._client = AsyncMock(return_value=bot)
    sender = PublicTelegramSender(app, channel)
    deliveries = [delivery(1), delivery(2)]
    chunk = format_digest_chunks(deliveries)[0]
    result = await sender.send_digest(deliveries, chunk)
    assert result.status == "sent" and result.message_id == "44"
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.args[:2] == ("100", chunk.text)


@pytest.mark.asyncio
async def test_digest_sender_refuses_mixed_and_rebound_destinations(public_v2_system):
    app, _, _, _ = public_v2_system
    await app.dispatcher.stop()
    channel = MagicMock()
    channel._client = AsyncMock()
    sender = PublicTelegramSender(app, channel)
    deliveries = [delivery(1), delivery(2)]
    chunk = format_digest_chunks(deliveries)[0]
    assert (
        await sender.send_digest([deliveries[0], replace(deliveries[1], user_id=20)], chunk)
    ).status == "dead_letter"
    await app.subscription_repo.bind_telegram_chat(10, 101)
    assert (await sender.send_digest(deliveries, chunk)).status == "dead_letter"
    channel._client.assert_not_awaited()


@pytest.mark.asyncio
async def test_digest_sender_preserves_ambiguous_timeout(public_v2_system):
    app, _, _, _ = public_v2_system
    await app.dispatcher.stop()
    bot = AsyncMock()
    bot.send_message.side_effect = TimeoutError("injected timeout")
    channel = MagicMock()
    channel._client = AsyncMock(return_value=bot)
    sender = PublicTelegramSender(app, channel)
    deliveries = [delivery(1), delivery(2)]
    assert (
        await sender.send_digest(deliveries, format_digest_chunks(deliveries)[0])
    ).status == "ambiguous"
