"""Helper functions for on_photo_attachment (slice 204)."""

from __future__ import annotations

from typing import Any

from aiogram.types import Message

from hunter.core.models import User


async def handle_photo_attachment_message(
    message: Message,
    current_user: User,
    db_inst: Any,
) -> None:
    await message.reply(
        "📷 В LotRadar v2 поиск работает по прямым ссылкам и фильтрам каталога Авито.\n"
        "Загрузка фото-эталонов больше не требуется. Для добавления поиска используйте команду /add.",
    )
