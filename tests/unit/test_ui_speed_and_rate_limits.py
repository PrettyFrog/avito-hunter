"""Tests for UI responsiveness optimizations and 60 req/min rate limit."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import CallbackQuery

from hunter.notify.public_telegram_sender import PublicTelegramSender
from hunter.notify.security import GLOBAL_STRANGER_LIMITER, AuthAndScopeMiddleware, StrangerLimiter
from hunter.notify.telegram import TelegramChannel


def test_stranger_limiter_defaults_to_60() -> None:
    limiter = StrangerLimiter()
    assert limiter.user_rate_limit == 60
    assert GLOBAL_STRANGER_LIMITER.user_rate_limit == 60


def test_public_telegram_sender_heartbeat_default_is_10s() -> None:
    sender = PublicTelegramSender(bootstrap=MagicMock(), channel=MagicMock())
    assert sender.heartbeat_interval == 10.0


@pytest.mark.asyncio
async def test_telegram_channel_session_keepalive_timeout_is_60s() -> None:
    settings = MagicMock()
    settings.telegram_bot_token = "123:ABC"
    settings.telegram_chat_id = "1001"
    settings.telegram_api_base = None
    settings.http_proxy = "http://127.0.0.1:10808"
    settings.telegram_egress = None
    settings.public_v2 = MagicMock(enabled=False)
    channel = TelegramChannel(settings=settings)
    bot = await channel._client()
    assert bot.session._connector_init.get("keepalive_timeout") == 60.0
    await bot.session.close()


@pytest.mark.asyncio
async def test_middleware_answers_callback_on_rate_limit() -> None:
    limiter = StrangerLimiter(user_rate_limit=1)
    db = MagicMock()
    db.get_user_by_telegram_id = AsyncMock(
        return_value={
            "id": 1,
            "chat_id": 2002,
            "telegram_user_id": 2002,
            "role": "owner",
            "status": "active",
            "created_at": "2026-01-01T00:00:00Z",
        }
    )
    db.update_user_last_seen = AsyncMock()
    mw = AuthAndScopeMiddleware(owner_chat_id="2002", limiter=limiter, db=db)

    callback = MagicMock(spec=CallbackQuery)
    callback.chat = MagicMock(type="private")
    callback.from_user = MagicMock(id=2002)
    callback.answer = AsyncMock()

    handler = AsyncMock(return_value="handled")

    res1 = await mw(handler, callback, {})
    assert res1 == "handled"

    # Second request exceeds rate limit:
    res2 = await mw(handler, callback, {})
    assert res2 is None
    callback.answer.assert_awaited_with(
        "Слишком частые запросы. Подождите минуту.", show_alert=True
    )
