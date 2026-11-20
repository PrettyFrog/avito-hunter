"""Telegram command for issuing a single-use VK binding token."""

from __future__ import annotations

from typing import Any

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from hunter.core.models import User
from hunter.notify.security import requires


def register_vk_binding_command(router: Router, binding_service: Any) -> None:
    """Expose a short-lived bearer token only to an authenticated private chat."""

    @router.message(Command("bind_vk"))
    @requires("user")
    async def bind_vk(message: Message, current_user: User | None = None) -> None:
        if not binding_service:
            await message.reply("Привязка VK сейчас не настроена.")
            return
        if not message.from_user or message.chat.type != "private":
            await message.reply("Запросите токен в личном чате с ботом.")
            return
        token = binding_service.generate_token(current_user.id)
        await message.reply(
            "Отправьте это сообщение сообществу VK в личные сообщения в течение 10 минут:\n"
            f"<code>{token}</code>",
            parse_mode="HTML",
        )
