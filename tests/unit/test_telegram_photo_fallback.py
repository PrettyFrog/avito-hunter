import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from hunter.notify.base import NotificationEvent
from hunter.notify.telegram import TelegramChannel


@pytest.mark.asyncio
async def test_telegram_send_photo_bad_request_falls_back_to_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    settings = MagicMock()
    settings.telegram_bot_token = "123456:FAKE_TOKEN"
    settings.telegram_chat_id = "1001"
    settings.http_proxy = None
    settings.data_dir = tmp_path
    settings.images_dir = tmp_path / "images"
    monkeypatch.setattr("hunter.notify.telegram.get_settings", lambda: settings)

    img_dir = settings.images_dir / "12345678"
    img_dir.mkdir(parents=True)
    (img_dir / "img_0.jpg").write_bytes(b"fake_image_bytes")

    channel = TelegramChannel()
    assert channel.enabled() is True

    mock_bot = AsyncMock()
    mock_bot.send_photo.side_effect = TelegramBadRequest(
        method=MagicMock(),
        message="Bad Request: failed to get HTTP URL content",
    )
    mock_bot.send_message.return_value = MagicMock(message_id=123)

    channel._bot = mock_bot

    event = NotificationEvent(
        avito_id="12345678",
        title="Apple iPhone 17 256GB",
        price=60000,
        max_price=65000,
        watchlist_id="u1_iphone_17",
        category="phones",
        model_id="iphone_17",
        verdict="target_model",
        reasons=["title match"],
        url="https://www.avito.ru/12345678",
        recipient_chat_id=1001,
        recipient_user_id=1,
    )

    success = await channel.send(event)
    assert success is True
    mock_bot.send_photo.assert_called_once()
    mock_bot.send_message.assert_called_once()


@pytest.mark.asyncio
async def test_telegram_send_photo_user_blocked_does_not_fall_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    settings = MagicMock()
    settings.telegram_bot_token = "123456:FAKE_TOKEN"
    settings.telegram_chat_id = "1001"
    settings.http_proxy = None
    settings.data_dir = tmp_path
    settings.images_dir = tmp_path / "images"
    monkeypatch.setattr("hunter.notify.telegram.get_settings", lambda: settings)

    img_dir = settings.images_dir / "12345678"
    img_dir.mkdir(parents=True)
    (img_dir / "img_0.jpg").write_bytes(b"fake_image_bytes")

    channel = TelegramChannel()

    mock_bot = AsyncMock()
    mock_bot.send_photo.side_effect = TelegramForbiddenError(
        method=MagicMock(),
        message="Forbidden: bot was blocked by the user",
    )

    channel._bot = mock_bot

    event = NotificationEvent(
        avito_id="12345678",
        title="Apple iPhone 17 256GB",
        price=60000,
        max_price=65000,
        watchlist_id="u1_iphone_17",
        category="phones",
        model_id="iphone_17",
        verdict="target_model",
        reasons=["title match"],
        url="https://www.avito.ru/12345678",
        recipient_chat_id=2002,
        recipient_user_id=2,
    )

    with patch.object(channel, "alert", new_callable=AsyncMock):
        success = await channel.send(event)
        assert success is False
        mock_bot.send_photo.assert_called_once()
        mock_bot.send_message.assert_not_called()
