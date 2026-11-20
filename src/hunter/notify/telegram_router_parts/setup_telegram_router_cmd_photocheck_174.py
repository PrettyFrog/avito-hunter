"""Telegram router parts: /photocheck and /add command routing (T-384)."""

from __future__ import annotations

from typing import Any

from aiogram.types import Message

from hunter.core.models import User
from hunter.notify.telegram_handlers.public_add_entry import maybe_start_public_add


async def handle_photocheck_command(
    message: Message, current_user: User, store: Any, settings: Any
) -> None:
    await message.reply(
        "ℹ️ Диагностика фото (/photocheck) устарела в LotRadar v2. Поиск лотов ведется по прямым фильтрам каталога Авито.",
    )


async def handle_add_command(
    message: Message,
    current_user: User,
    db_inst: Any,
    store: Any,
    start_add_verification_fn: Any,
) -> None:
    await maybe_start_public_add(message, current_user, db_inst)
