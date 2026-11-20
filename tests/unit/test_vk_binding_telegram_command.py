"""The authenticated Telegram router issues tenant-scoped VK binding tokens."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import Chat, Message
from aiogram.types import User as TelegramUser

from hunter.application.channel_binding import ChannelBindingService
from hunter.core.db import Database
from hunter.notify.telegram_router import setup_telegram_router


@pytest.mark.asyncio
async def test_bind_vk_command_issues_token_to_authenticated_private_user(tmp_path: Path) -> None:
    db = Database(tmp_path / "binding-command.db")
    await db.connect()
    try:
        await db.create_user(telegram_user_id=8801, chat_id=8801, role="user", status="active")
        service = SimpleNamespace(
            channel_binding_service=ChannelBindingService("private-secret-" + "x" * 32)
        )
        bot = AsyncMock()
        message = Message(
            message_id=1,
            date=int(datetime.now(UTC).timestamp()),
            chat=Chat(id=8801, type="private"),
            from_user=TelegramUser(id=8801, is_bot=False, first_name="User"),
            text="/bind_vk",
        )
        message.as_(bot)
        object.__setattr__(message, "reply", AsyncMock(return_value=True))
        router = setup_telegram_router(
            SimpleNamespace(chat_id="9999"), db=db, subscription_service=service
        )

        await router.propagate_event("message", message, db=db, bot=bot)

        message.reply.assert_awaited_once()
        text = message.reply.await_args.args[0]
        assert "Отправьте это сообщение сообществу VK" in text
        token = text.split("<code>", 1)[1].split("</code>", 1)[0]
        service.channel_binding_service.verify_token(token, expected_user_id=1)
    finally:
        await db.close()
