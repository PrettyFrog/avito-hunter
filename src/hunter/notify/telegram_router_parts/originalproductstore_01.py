(
    'Telegram router parts: OriginalProductStore, labels, chat locks, safe '
    'messaging, and forecast (T-352).'
)

from __future__ import annotations

import asyncio
from typing import Any

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message
from loguru import logger

from hunter.core.product import ProductProfile, ProductStore

_OriginalProductStore = ProductStore

_LABELS = {
    "target_model": "✅ та модель",
    "wrong_model": "❌ не та",
    "ok": "👍 норм",
    "fake": "🚫 фейк",
    "expensive": "📈 дорого",
}
_COMMAND_THROTTLE_SEC = 0.5
_MAX_PHOTO_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB

_chat_locks: dict[tuple[int, int], asyncio.Lock] = {}
_ADMIN_CONFIRMATIONS: dict[tuple[int, int], dict[str, Any]] = {}
_BROADCAST_PAYLOADS: dict[int, str] = {}


def _get_chat_lock(chat_id: int) -> asyncio.Lock:
    try:
        loop = asyncio.get_running_loop()
        loop_id = id(loop)
    except RuntimeError:
        loop_id = 0
    key = (loop_id, chat_id)
    if key not in _chat_locks:
        _chat_locks[key] = asyncio.Lock()
    return _chat_locks[key]


async def _safe_reply(
    message: Message,
    text: str,
    *,
    parse_mode: str | None = None,
    disable_web_page_preview: bool | None = None,
    reply_markup: Any = None,
) -> Message:
    try:
        return await message.reply(
            text,
            parse_mode=parse_mode,
            disable_web_page_preview=disable_web_page_preview,
            reply_markup=reply_markup,
        )
    except TelegramBadRequest as exc:
        if "parse entities" in str(exc).lower() or "entity" in str(exc).lower():
            logger.warning(
                'Telegram parse entities error on reply, falling back to plain text: '
                '{}', exc)
            return await message.reply(
                text,
                parse_mode=None,
                disable_web_page_preview=disable_web_page_preview,
                reply_markup=reply_markup,
            )
        raise


async def _safe_edit_text(
    target: Any,
    text: str,
    *,
    parse_mode: str | None = None,
    reply_markup: Any = None,
) -> Any:
    try:
        return await target.edit_text(
            text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
        )
    except TelegramBadRequest as exc:
        if "parse entities" in str(exc).lower() or "entity" in str(exc).lower():
            logger.warning("Telegram parse entities error on edit_text, falling back to plain text: {}", exc)
            return await target.edit_text(
                text,
                parse_mode=None,
                reply_markup=reply_markup,
            )
        raise


def calculate_profiles_forecast_worst_case(profiles: list[ProductProfile]) -> float:
    total = 0.0
    for p in profiles:
        if p.mode == "off":
            continue
        if getattr(p, "poll_seconds", None):
            daily_polls = 75600.0 / float(p.poll_seconds)
        elif p.poll_minutes:
            daily_polls = (21.0 * 60.0) / float(p.poll_minutes)
        else:
            default_intervals = {"high": 15.0, "normal": 30.0, "low": 120.0}
            intv = default_intervals.get(p.priority, 30.0)
            daily_polls = (21.0 * 60.0) / intv
        max_cards = getattr(getattr(p, "limits", None), "max_cards_per_day", None)
        if max_cards is None:
            daily_cards = 15.0 if p.match.policy == "loose" else 5.0
        else:
            daily_cards = float(min(max_cards, 20))
        photo_est = 1.1 if p.id.startswith("gem_") else 0.0
        total += daily_polls + daily_cards + photo_est
    return total + 15.0  # +15 worst-case retries margin


def attach_auth_middleware(router: Any, owner_chat_id: str, db: Any = None) -> Any:
    """Attach AuthAndScopeMiddleware to message, callback, and chat member events."""
    import os
    from hunter.notify.security import AuthAndScopeMiddleware, StrangerLimiter

    limiter = (
        StrangerLimiter(user_rate_limit=10000, stranger_cooldown_sec=0.0)
        if os.environ.get("HUNTER_STAND_TEST")
        else None
    )
    auth_mw = AuthAndScopeMiddleware(owner_chat_id=owner_chat_id, limiter=limiter, db=db)
    router.message.outer_middleware(auth_mw)
    router.message.middleware(auth_mw)
    router.callback_query.outer_middleware(auth_mw)
    router.callback_query.middleware(auth_mw)
    router.my_chat_member.outer_middleware(auth_mw)
    router.my_chat_member.middleware(auth_mw)
    return auth_mw


def resolve_db_helper(injected_db: Any = None, fallback_db: Any = None) -> Any:
    """Resolve database instance from injected, fallback, or singleton."""
    from hunter.core.db import get_db

    return injected_db or fallback_db or get_db()


def resolve_user_helper(user: Any = None, owner_chat_id: str | int = 0) -> Any:
    """Resolve user instance, falling back to owner user."""
    from hunter.core.models import User, now_utc

    if user is not None:
        return user
    return User(
        id=1,
        telegram_user_id=int(owner_chat_id),
        chat_id=int(owner_chat_id),
        role="owner",
        status="active",
        created_at=now_utc().isoformat(),
    )


def resolve_store_helper() -> Any:
    """Resolve ProductStore instance, checking for test overrides."""
    from hunter.core.product import ProductStore, get_product_store

    if ProductStore is not _OriginalProductStore:
        return ProductStore()
    return get_product_store()


def main_menu_keyboard() -> Any:
    """Return default main menu reply keyboard."""
    from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Добавить товар"), KeyboardButton(text="📋 Мои товары")],
            [KeyboardButton(text="❓ Помощь")],
        ],
        resize_keyboard=True,
    )


async def check_user_blocked_status(
    db_inst: Any,
    message: Message,
    user_id: int,
    current_user: Any,
) -> tuple[bool, Any]:
    """Check if user is blocked or inactive, and reactivate if bot_blocked.
    Returns (should_stop, updated_user).
    """
    from hunter.core.models import User
    from hunter.notify.messages_ru import USER_BLOCKED_BY_ADMIN

    user_data = await db_inst.get_user_by_telegram_id(user_id)
    if user_data:
        st = user_data.get("status")
        if st == "blocked_by_owner":
            await message.reply(USER_BLOCKED_BY_ADMIN)
            return True, current_user
        elif st in ("blocked", "inactive"):
            return True, current_user
        elif st == "bot_blocked":
            await db_inst.set_user_status(user_data["id"], "active")
            user_data["status"] = "active"
            current_user = User(**user_data)

    if current_user is None and user_data:
        current_user = User(**user_data)

    return False, current_user


def parse_start_args(command: Any, message_text: str | None) -> str | None:
    """Extract start command arguments."""
    args = command.args if command else None
    if not args and message_text:
        text_parts = message_text.split(maxsplit=1)
        if len(text_parts) > 1:
            args = text_parts[1].strip()
    return args


async def validate_start_invite(
    db_inst: Any,
    message: Message,
    raw_token: str,
    user_id: int,
    current_user: Any,
) -> tuple[bool, dict[str, Any] | None, str]:
    """Validate invite token for /start command.
    Returns (valid, invite_dict_or_none, token_hash).
    """
    import hashlib
    from hunter.core.models import now_utc
    from hunter.notify.messages_ru import (
        INVITE_ALREADY_USED,
        INVITE_EXPIRED,
        INVITE_NOT_FOUND,
        INVITE_REVOKED,
        KNOWN_USER_GREETING_TEMPLATE,
        clean_name,
    )
    from hunter.notify.security import GLOBAL_STRANGER_LIMITER

    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    invite = await db_inst.get_invite_by_hash(token_hash)
    if not invite:
        GLOBAL_STRANGER_LIMITER.record_invalid_invite_attempt(user_id)
        await message.reply(INVITE_NOT_FOUND, parse_mode="HTML")
        return False, None, token_hash

    if invite.get("revoked", 0) == 1:
        await message.reply(INVITE_REVOKED, parse_mode="HTML")
        return False, None, token_hash

    now_str = now_utc().isoformat()
    if invite.get(
        'used_'
        'at') is not None or invite.get("used_count", 0) >= invite.get("max_uses", 1):
        await message.reply(INVITE_ALREADY_USED, parse_mode="HTML")
        return False, None, token_hash

    if invite.get("expires_at", "") <= now_str:
        await message.reply(INVITE_EXPIRED, parse_mode="HTML")
        return False, None, token_hash

    if current_user is not None and current_user.status == "active":
        await message.reply(
            f"{KNOWN_USER_GREETING_TEMPLATE.format(name=clean_name(current_user.display_name))}\nВы уже зарегистрированы в боте.",
            reply_markup=main_menu_keyboard(),
            parse_mode="HTML",
        )
        return False, None, token_hash

    return True, invite, token_hash
