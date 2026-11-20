"""send_broadcast helper functions (part 1)."""

from __future__ import annotations

import asyncio
from typing import Any

from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from loguru import logger

from hunter.core.db import Database


async def resolve_broadcast_bot(bot: Any = None) -> Any:
    if bot is None:
        try:
            from hunter.notify.telegram import TelegramChannel

            ch = TelegramChannel()
            if ch.enabled():
                bot = await ch._client()
        except Exception as exc:
            logger.debug("Could not initialize TelegramChannel for broadcast: {}", exc)

    if bot is None or not hasattr(bot, "send_message"):
        logger.warning("No telegram bot available for broadcast")
        return None
    return bot


async def get_broadcast_recipients(
    db: Database, target_audience: str
) -> list[dict[str, Any]]:
    conn = await db.connect()
    if target_audience in ("family", "friend"):
        cur = await conn.execute(
            "SELECT id, chat_id FROM users WHERE status = 'active' AND chat_id IS NOT "
                'NULL AND chat_id != 0 AND tier = ?',
            (target_audience,),
        )
    else:
        cur = await conn.execute(
            "SELECT id, chat_id FROM users WHERE status = 'active' AND chat_id IS NOT "
                'NULL AND chat_id != 0'
        )
    return [dict(r) for r in await cur.fetchall()]


async def deliver_broadcast_to_user(
    bot: Any,
    db: Database,
    user: dict[str, Any],
    text: str,
) -> bool:
    uid = user["id"]
    cid = user["chat_id"]
    attempts = 0
    while attempts < 3:
        attempts += 1
        try:
            await bot.send_message(
                chat_id=cid,
                text=text,
                parse_mode=None,
                disable_web_page_preview=True,
            )
            return True
        except TelegramRetryAfter as exc:
            wait_sec = float(getattr(exc, "retry_after", 1.0)) + 0.5
            logger.warning("Telegram rate limited broadcast, sleeping {}s", wait_sec)
            await asyncio.sleep(wait_sec)
        except TelegramForbiddenError as exc:
            logger.warning("User {} blocked bot during broadcast: {}", uid, exc)
            await db.mark_user_bot_blocked(uid)
            return False
        except Exception as exc:
            err_str = str(exc).lower()
            if "chat not found" in err_str or "deactivated" in err_str or "blocked" in err_str:
                logger.warning("User {} chat unavailable during broadcast: {}", uid, exc)
                await db.mark_user_bot_blocked(uid)
            else:
                logger.warning("Failed broadcast message to user {} chat {}: {}", uid, cid, exc)
            return False
    return False


async def dispatch_broadcast_to_recipients(
    bot: Any,
    db: Database,
    users: list[dict[str, Any]],
    text: str,
    owner_id: int | None,
) -> tuple[int, int]:
    delivered = 0
    failed = 0
    for u in users:
        if owner_id and u["id"] == owner_id:
            continue
        success = await deliver_broadcast_to_user(bot, db, u, text)
        if success:
            delivered += 1
        else:
            failed += 1
        await asyncio.sleep(0.05)
    return delivered, failed
