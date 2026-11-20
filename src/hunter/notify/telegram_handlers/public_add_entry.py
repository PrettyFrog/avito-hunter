"""Select public add consistently for commands and home-screen callbacks."""
from typing import Any

from hunter.core.models import User
from hunter.notify.telegram_handlers.public_add import start_public_add


async def maybe_start_public_add(message: Any, user: User, db: Any) -> bool:
    await start_public_add(message, db, user)
    return True
