"""Утилиты для единых системных оповещений (бродкастов) пользователям."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from loguru import logger

from hunter.core.config import get_settings
from hunter.core.db import Database
from hunter.notify.broadcast_parts.send_broadcast_01 import (
    dispatch_broadcast_to_recipients,
    get_broadcast_recipients,
    resolve_broadcast_bot,
)
from hunter.notify.messages_ru import BROADCAST_PAUSED_ALL, BROADCAST_RESUMED_ALL

__all__ = [
    "get_broadcast_target_counts",
    "send_broadcast",
    "broadcast_to_users",
    "set_global_pause_with_broadcast",
]


async def get_broadcast_target_counts(db: Database) -> dict[str, int]:
    (
        "Returns number of active recipients for 'all', 'family', and 'friend' "
        'tiers, excluding owner.'
    )
    conn = await db.connect()
    owner = await db.get_owner_user()
    owner_id = owner.get("id") if owner else -1

    cur = await conn.execute(
        "SELECT tier, COUNT(*) FROM users WHERE status = 'active' AND chat_id IS "
            'NOT NULL AND chat_id != 0 AND id != ? GROUP BY tier',
        (owner_id,),
    )
    rows = await cur.fetchall()
    counts = {"all": 0, "family": 0, "friend": 0}
    for tier, cnt in rows:
        c = int(cnt)
        if tier == "family":
            counts["family"] = c
        elif tier == "friend":
            counts["friend"] = c
        counts["all"] += c
    return counts


async def send_broadcast(
    db: Database,
    text: str,
    target_audience: str = "all",  # 'all', 'family', 'friend'
    bot: Any = None,
    sender_user_id: int | None = None,
) -> tuple[int, int]:
    """Sends rate-limited plain text broadcast (<= 20 msg/sec) to active users.
    - Plain text without markup (parse_mode=None).
    - Rate limit: max 20 messages per second (50ms sleep between sends).
    - Pauses on TelegramRetryAfter.
    - Skips inactive, blocked_by_owner, bot_blocked, deleted.
    - Marks user as bot_blocked on TelegramForbiddenError (403).
    - Records broadcast to broadcasts table in DB (who, when, delivered, failed).
    Returns (delivered_count, failed_count).
    """
    bot = await resolve_broadcast_bot(bot)
    if bot is None:
        return 0, 0

    users = await get_broadcast_recipients(db, target_audience)
    owner = await db.get_owner_user()
    owner_id = owner.get("id") if owner else None

    delivered, failed = await dispatch_broadcast_to_recipients(
        bot=bot,
        db=db,
        users=users,
        text=text,
        owner_id=owner_id,
    )

    await db.log_broadcast(
        sender_user_id=sender_user_id,
        target_audience=target_audience,
        text=text,
        delivered_count=delivered,
        failed_count=failed,
    )
    return delivered, failed


async def broadcast_to_users(
    db: Database,
    text: str,
    bot: Any = None,
    exclude_owner: bool = True,
) -> int:
    """Sends a single broadcast message to all active users."""
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
        return 0

    chat_ids = await db.get_active_user_chat_ids()
    owner = await db.get_owner_user()
    owner_chat_id = owner["chat_id"] if (owner and exclude_owner) else None

    sent = 0
    for cid in chat_ids:
        if owner_chat_id and cid == owner_chat_id:
            continue
        try:
            await bot.send_message(chat_id=cid, text=text)
            sent += 1
        except Exception as exc:
            logger.debug("Failed broadcast message to chat {}: {}", cid, exc)
    return sent


async def set_global_pause_with_broadcast(db: Database, paused: bool, bot: Any = None) -> bool:
    """Sets or clears global pause and sends a single broadcast to active users if state changed."""
    notified = await db.get_runtime("users_notified_paused", "0")
    settings = get_settings()
    data_dir = Path(os.environ.get("HUNTER_DATA_DIR") or settings.data_dir)
    flag_pause = data_dir / "pause.flag"
    flag_captcha = data_dir / "captcha.flag"

    if paused:
        await db.set_runtime("paused", "1")
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
            flag_pause.touch(exist_ok=True)
        except Exception:
            pass
        if notified != "1":
            await db.set_runtime("users_notified_paused", "1")
            await broadcast_to_users(db, BROADCAST_PAUSED_ALL, bot=bot, exclude_owner=True)
            return True
    else:
        await db.set_runtime("paused", "0")
        await db.set_runtime("captcha", "0")
        await db.clear_all_pauses()
        try:
            flag_pause.unlink(missing_ok=True)
            flag_captcha.unlink(missing_ok=True)
        except Exception:
            pass
        if notified == "1":
            await db.set_runtime("users_notified_paused", "0")
            await broadcast_to_users(db, BROADCAST_RESUMED_ALL, bot=bot, exclude_owner=True)
            return True
    return False
