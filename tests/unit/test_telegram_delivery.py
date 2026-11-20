"""Unit tests for typed Telegram delivery adapter (T-486)."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)

from hunter.core.config import Settings
from hunter.notify.telegram import TelegramChannel, TelegramRelaySession
from hunter.notify.telegram_delivery import (
    TelegramDeliveryAdapter,
    TelegramDeliveryEnvelope,
    send_telegram_envelope,
)
from hunter.storage.deliveries import ClaimedDelivery


@pytest.mark.asyncio
async def test_telegram_delivery_success_and_acceptance_id() -> None:
    bot = AsyncMock()
    bot.send_message.return_value = MagicMock(message_id=12345)
    env = TelegramDeliveryEnvelope(chat_id=101, text="Test message")
    res = await send_telegram_envelope(bot, env)
    assert res.status == "sent" and res.message_id == "12345"

    adapter = TelegramDeliveryAdapter(bot)
    claimed = ClaimedDelivery(
        "d1",
        "e1",
        101,
        "telegram",
        1,
        {"title": "Item", "price": 1000, "url": "https://a.ru/1"},
        1,
        "tok",
        "t",
    )
    s_res = await adapter.send_delivery(claimed, destination=101)
    assert s_res.status == "sent" and s_res.message_id == "12345"


@pytest.mark.asyncio
async def test_telegram_delivery_retry_after_extracted() -> None:
    bot = AsyncMock()
    bot.send_message.side_effect = TelegramRetryAfter(
        method=None, message="Too Many Requests", retry_after=45
    )
    res = await send_telegram_envelope(bot, TelegramDeliveryEnvelope(chat_id=101, text="Hello"))
    assert res.status == "retry" and res.retry_after == 45
    assert res.to_send_result().retry_scope == "chat"


@pytest.mark.asyncio
async def test_telegram_delivery_blocked_and_forbidden() -> None:
    bot = AsyncMock()
    bot.send_message.side_effect = TelegramForbiddenError(
        method=None, message="Forbidden: bot was blocked by the user"
    )
    res1 = await send_telegram_envelope(bot, TelegramDeliveryEnvelope(chat_id=101, text="Hello"))
    assert res1.status == "blocked"

    bot.send_message.side_effect = TelegramBadRequest(
        method=None, message="Bad Request: chat not found"
    )
    res2 = await send_telegram_envelope(bot, TelegramDeliveryEnvelope(chat_id=101, text="Hello"))
    assert res2.status == "blocked"


@pytest.mark.asyncio
async def test_telegram_delivery_ambiguous_and_permanent() -> None:
    bot = AsyncMock()
    bot.send_message.side_effect = TelegramNetworkError(method=None, message="Socket dropped")
    res1 = await send_telegram_envelope(bot, TelegramDeliveryEnvelope(chat_id=101, text="Hello"))
    assert res1.status == "ambiguous"
    assert res1.to_send_result().retry_scope == "delivery"

    bot.send_message.side_effect = TelegramBadRequest(
        method=None, message="Bad Request: message text is empty"
    )
    res2 = await send_telegram_envelope(bot, TelegramDeliveryEnvelope(chat_id=101, text="Hello"))
    assert res2.status == "permanent"


@pytest.mark.asyncio
async def test_telegram_delivery_photo_fallback_captures_acceptance_id() -> None:
    bot = AsyncMock()
    bot.send_photo.side_effect = TelegramBadRequest(
        method=None, message="Bad Request: failed to get HTTP URL content"
    )
    bot.send_message.return_value = MagicMock(message_id=9876)
    env = TelegramDeliveryEnvelope(chat_id=101, text="Caption", photo_input=b"dummy")
    res = await send_telegram_envelope(bot, env)
    assert res.status == "sent" and res.message_id == "9876"
    bot.send_photo.assert_called_once()
    bot.send_message.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("photo", [False, True])
@pytest.mark.parametrize("message_id", [None, 0, -1])
async def test_missing_telegram_message_id_is_ambiguous(photo, message_id):
    bot = AsyncMock()
    reply = MagicMock(message_id=message_id)
    bot.send_photo.return_value = reply
    bot.send_message.return_value = reply
    env = TelegramDeliveryEnvelope(
        chat_id=101, text="Hello", photo_input=b"photo" if photo else None
    )
    result = await send_telegram_envelope(bot, env)
    assert result.status == "ambiguous"
    if photo:
        bot.send_message.assert_not_awaited()


def test_public_telegram_fails_closed_without_relay_even_with_local_proxy() -> None:
    settings = Settings(_env_file=None, telegram_bot_token="fake-token", telegram_chat_id="42")
    settings.public_v2.enabled = True
    settings.http_proxy = "http://127.0.0.1:10808"
    with pytest.raises(RuntimeError, match="HTTPS relay") as caught:
        TelegramChannel(settings)
    assert "fake-token" not in str(caught.value)


def test_public_telegram_allows_local_proxy_when_relay_not_required() -> None:
    settings = Settings(_env_file=None, telegram_bot_token="fake-token", telegram_chat_id="42")
    settings.public_v2.enabled = True
    settings.public_v2.require_telegram_relay = False
    settings.http_proxy = "http://127.0.0.1:10808"
    channel = TelegramChannel(settings)
    assert channel._relay_config is None
    assert channel.enabled() is True


def test_telegram_egress_loads_from_environment_as_secret(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_EGRESS__PRIMARY_ENDPOINT", "https://relay.example.test")
    monkeypatch.setenv("TELEGRAM_EGRESS__ALLOWED_HOSTS", '["relay.example.test"]')
    monkeypatch.setenv("TELEGRAM_EGRESS__HMAC_SECRET", "fake-hmac-secret")
    settings = Settings(_env_file=None)
    assert settings.telegram_egress.primary_endpoint == "https://relay.example.test"
    assert settings.telegram_egress.allowed_hosts == {"relay.example.test"}
    assert settings.telegram_egress.hmac_secret.get_secret_value() == "fake-hmac-secret"
    assert "fake-hmac-secret" not in repr(settings.telegram_egress)


@pytest.mark.asyncio
async def test_long_poll_and_sender_share_server_owned_relay_session(monkeypatch) -> None:
    from pydantic import SecretStr

    import hunter.notify.telegram as telegram_module
    from hunter.core.config import TelegramEgressConfig

    relays = []

    class FakeRelayClient:
        def __init__(self, config, *, secondary_config=None) -> None:
            self.config = config
            self.secondary_config = secondary_config
            self.calls = []
            relays.append(self)

        async def call(self, method, token, parameters):
            self.calls.append((method, token, parameters))
            if method == "getUpdates":
                return {"ok": True, "result": []}
            if parameters.get("text") == "forbidden":
                return {
                    "ok": False,
                    "error_code": 403,
                    "description": "Forbidden: bot was blocked by the user",
                }
            return {
                "ok": True,
                "result": {
                    "message_id": 9,
                    "date": 1700000000,
                    "chat": {"id": 42, "type": "private", "first_name": "User"},
                    "text": "hello",
                },
            }

        async def close(self):
            return None

    monkeypatch.setattr(telegram_module, "TelegramRelayClient", FakeRelayClient)
    settings = Settings(
        _env_file=None,
        telegram_bot_token="123456:fake-token",
        telegram_chat_id="42",
        telegram_egress=TelegramEgressConfig(
            primary_endpoint="https://relay.example.test",
            secondary_endpoint="https://backup.example.test",
            allowed_hosts={"relay.example.test", "backup.example.test"},
            hmac_secret=SecretStr("fake-hmac-secret"),
        ),
    )
    settings.public_v2.enabled = True
    settings.http_proxy = "http://127.0.0.1:10808"
    channel = TelegramChannel(settings)
    bot = await channel._client()
    assert await channel._client() is bot
    assert isinstance(bot.session, TelegramRelaySession)
    sent = await bot.send_message(42, "hello")
    updates = await bot.get_updates(timeout=1)
    assert sent.message_id == 9 and updates == []
    with pytest.raises(TelegramForbiddenError):
        await bot.send_message(42, "forbidden")
    assert len(relays) == 1
    assert relays[0].config.endpoint == "https://relay.example.test"
    assert relays[0].secondary_config.endpoint == "https://backup.example.test"
    assert channel._secondary_relay_config.endpoint == "https://backup.example.test"
    assert {call[0] for call in relays[0].calls} == {"sendMessage", "getUpdates"}
    assert all(call[1] == "123456:fake-token" for call in relays[0].calls)
    await bot.session.close()


@pytest.mark.asyncio
async def test_telegram_delivery_text_mode_when_photo_disabled() -> None:
    bot = AsyncMock()
    bot.send_message.return_value = MagicMock(message_id=9999)
    adapter = TelegramDeliveryAdapter(bot, send_photos=False)
    p = {"title": "iPhone 13", "price": 20000, "url": "https://a.ru/1", "image_url": "https://img/test.jpg"}
    res = await adapter.send_delivery(ClaimedDelivery("d", "e", 101, "telegram", 1, p, 1, "tok", "t"), 101)
    assert res.status == "sent" and res.message_id == "9999"
    bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_telegram_delivery_photo_and_fallback() -> None:
    bot = AsyncMock()
    bot.send_photo.return_value = MagicMock(message_id=9999)
    adapter = TelegramDeliveryAdapter(bot)
    p = {"title": "iPhone 13", "price": 20000, "url": "https://a.ru/1", "image_url": "https://img/640x480/t.jpg", "avito_id": "8577782054"}
    res = await adapter.send_delivery(ClaimedDelivery("d", "e", 101, "telegram", 1, p, 1, "tok", "t"), 101)
    assert res.status == "sent" and res.message_id == "9999"
    bot.send_photo.assert_awaited_once()

    # Fallback to text on bad request
    bot.send_photo.side_effect = TelegramBadRequest(method=MagicMock(), message="Bad Request: failed to get HTTP URL")
    bot.send_message.return_value = MagicMock(message_id=8888)
    res_fb = await adapter.send_delivery(ClaimedDelivery("d2", "e2", 101, "telegram", 1, p, 1, "tok", "t"), 101)
    assert res_fb.status == "sent" and res_fb.message_id == "8888"
    bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_telegram_channel_alert_retry_after_cooldown(monkeypatch) -> None:
    bot = AsyncMock()
    bot.send_message.side_effect = TelegramRetryAfter(
        method=None,
        message="Flood control",
        retry_after=60,
    )
    settings = Settings(_env_file=None, telegram_bot_token="fake-token", telegram_chat_id="101")
    settings.public_v2.enabled = True
    settings.public_v2.require_telegram_relay = False
    ch = TelegramChannel(settings)
    monkeypatch.setattr(ch, "_client", AsyncMock(return_value=bot))

    res1 = await ch.alert("Critical error")
    assert res1 is False
    assert bot.send_message.call_count == 1

    # Second alert during cooldown should immediately return False without calling bot
    res2 = await ch.alert("Second error")
    assert res2 is False
    assert bot.send_message.call_count == 1


@pytest.mark.asyncio
async def test_telegram_delivery_rich_and_fallbacks(monkeypatch) -> None:
    import hunter.notify.telegram_delivery as deliv_mod
    from aiogram.methods import SendRichMessage

    monkeypatch.setattr(deliv_mod, "fetch_photo_bytes", AsyncMock(return_value=b"fake_bytes"))
    bot = AsyncMock()
    bot.return_value = MagicMock(message_id=55555)
    adapter = TelegramDeliveryAdapter(bot)
    p = {"title": "AirPods", "price": 15000, "url": "https://a.ru/1", "image_url": "https://img/1.jpg"}
    res = await adapter.send_delivery(ClaimedDelivery("d", "e", 101, "telegram", 1, p, 1, "tok", "t"), 101)
    assert res.status == "sent" and res.message_id == "55555"
    assert isinstance(bot.call_args[0][0], SendRichMessage)

    # Fallback rich (RetryAfter or BadRequest) -> photo -> text
    env = TelegramDeliveryEnvelope(chat_id=101, text="Caption", photo_bytes=b"jpg", rich_html="<h2>Test</h2>")
    bot.side_effect = TelegramRetryAfter(method=MagicMock(), message="Flood control", retry_after=60000)
    bot.send_photo.return_value = MagicMock(message_id=66666)
    res_photo = await send_telegram_envelope(bot, env)
    assert res_photo.status == "sent" and res_photo.message_id == "66666"

    bot.send_photo.side_effect = TelegramBadRequest(method=MagicMock(), message="Bad Photo")
    bot.send_message.return_value = MagicMock(message_id=77777)
    res_text = await send_telegram_envelope(bot, env)
    assert res_text.status == "sent" and res_text.message_id == "77777"


@pytest.mark.asyncio
async def test_fetch_photos_bytes_parallel(monkeypatch) -> None:
    import hunter.notify.telegram_delivery as deliv_mod

    async def _mock_fetch(url: str, timeout: float = 4.0, client=None) -> bytes | None:
        return f"bytes_{url}".encode() if "fail" not in url else None

    monkeypatch.setattr(deliv_mod, "fetch_photo_bytes", _mock_fetch)
    res = await deliv_mod.fetch_photos_bytes(["http://1.jpg", "http://fail.jpg", "http://2.jpg"])
    assert res == [b"bytes_http://1.jpg", b"bytes_http://2.jpg"]


@pytest.mark.asyncio
async def test_fetch_photo_bytes_uses_shared_client() -> None:
    import hunter.notify.telegram_delivery as deliv_mod

    mock_resp = MagicMock(status_code=200, content=b"shared_bytes")
    mock_client = AsyncMock()
    mock_client.get.return_value = mock_resp

    data = await deliv_mod.fetch_photo_bytes("https://img/test.jpg", client=mock_client)
    assert data == b"shared_bytes"
    mock_client.get.assert_awaited_once()

    photos = await deliv_mod.fetch_photos_bytes(["https://img/test.jpg"], client=mock_client)
    assert photos == [b"shared_bytes"]


@pytest.mark.asyncio
async def test_telegram_delivery_adapter_passes_photo_client() -> None:
    bot = AsyncMock()
    bot.return_value = MagicMock(message_id=999)
    bot.send_photo.return_value = MagicMock(message_id=999)
    mock_client = AsyncMock()
    mock_client.get.return_value = MagicMock(status_code=200, content=b"img_bytes")

    adapter = TelegramDeliveryAdapter(bot, photo_client=mock_client)
    claimed = ClaimedDelivery(
        "d_img", "e1", 101, "telegram", 1,
        {"title": "Item", "price": 1000, "url": "https://a.ru/1", "photos": ["https://img.st/1.jpg"]},
        1, "tok", "t",
    )
    res = await adapter.send_delivery(claimed, destination=101)
    assert res.status == "sent"
    mock_client.get.assert_awaited()


@pytest.mark.asyncio
async def test_public_telegram_sender_reuses_photo_client_and_closes() -> None:
    from hunter.notify.public_telegram_sender import PublicTelegramSender

    bot = AsyncMock()
    bot.send_message.return_value = MagicMock(message_id=123)
    channel = MagicMock()
    channel._client = AsyncMock(return_value=bot)

    mock_client = AsyncMock()
    sender = PublicTelegramSender(bootstrap=MagicMock(), channel=channel, photo_client=mock_client)
    assert sender._get_photo_client() is mock_client

    await sender.aclose()
    mock_client.aclose.assert_awaited_once()
    assert sender._photo_client is None


@pytest.mark.asyncio
async def test_public_telegram_sender_heartbeat_keeps_connection_alive() -> None:
    import asyncio
    from hunter.notify.public_telegram_sender import PublicTelegramSender

    bot = AsyncMock()
    bot.get_me = AsyncMock()
    channel = MagicMock()
    channel._client = AsyncMock(return_value=bot)

    sender = PublicTelegramSender(
        bootstrap=MagicMock(), channel=channel, heartbeat_interval=0.01
    )
    sender.start_heartbeat()
    assert sender._heartbeat_task is not None

    await asyncio.sleep(0.03)
    bot.get_me.assert_awaited()

    await sender.aclose()
    assert sender._heartbeat_task is None






