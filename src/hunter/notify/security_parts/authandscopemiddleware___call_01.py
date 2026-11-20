"""AuthAndScopeMiddleware.__call__ helper functions (part 1)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, ChatMemberUpdated, Message, TelegramObject
from loguru import logger

from hunter.core.models import User


async def check_scope_and_rate(
    event: TelegramObject,
    data: dict[str, Any],
    limiter: Any,
) -> int | None:
    chat = getattr(event, "chat", None)
    if chat is None and hasattr(event, "message") and getattr(event, "message", None):
        chat = getattr(event.message, "chat", None)

    if chat and getattr(chat, "type", "private") != "private":
        logger.debug(
            "Silently dropping update from non-private chat type={}",
            getattr(chat, "type", None),
        )
        return None

    from_user = getattr(event, "from_user", None)
    if not from_user:
        return None

    user_id = from_user.id
    if not data.get("_rate_checked"):
        data["_rate_checked"] = True
        if not limiter.check_user_rate(user_id):
            logger.warning("Rate limit exceeded for user {}", user_id)
            if isinstance(event, CallbackQuery) or hasattr(event, "answer"):
                try:
                    await event.answer(
                        "Слишком частые запросы. Подождите минуту.", show_alert=True
                    )
                except Exception:
                    pass
            return None

    return user_id


async def resolve_or_seed_user(
    user_id: int,
    from_user: Any,
    owner_chat_id: str,
    db: Any,
) -> User | None:
    if str(user_id) == owner_chat_id:
        user_data = await db.get_user_by_telegram_id(user_id)
        if not user_data:
            db_id = await db.create_user(
                telegram_user_id=user_id,
                chat_id=user_id,
                role="owner",
                username=from_user.username,
                display_name=from_user.full_name or from_user.first_name,
                status="active",
            )
            user_data = await db.get_user_by_id(db_id)
        current_user = User(**user_data) if user_data else None
        if current_user:
            try:
                await db.update_user_last_seen(current_user.id)
            except Exception as exc:
                logger.debug("Failed updating user last_seen in seed: {}", exc)
        return current_user

    user_data = await db.get_user_by_telegram_id(user_id)
    return User(**user_data) if user_data else None


async def check_user_status_allowed(
    current_user: User | None,
    user_id: int,
    event: TelegramObject,
) -> bool:
    if not current_user:
        return True

    if current_user.status == "blocked_by_owner":
        logger.warning("User {} blocked_by_owner update dropped", user_id)
        if isinstance(event, Message):
            try:
                from hunter.notify.messages_ru import USER_BLOCKED_BY_ADMIN

                await event.reply(USER_BLOCKED_BY_ADMIN)
            except Exception:
                pass
        return False

    if current_user.status == "bot_blocked":
        msg_text = getattr(event, "text", "") or ""
        if not (isinstance(event, Message) and msg_text.startswith("/start")):
            if isinstance(event, ChatMemberUpdated) and getattr(
                getattr(event, "new_chat_member", None), "status", None
            ) == "member":
                return True
            logger.warning(
                "bot_blocked user {} sent non-start message, dropped", user_id
            )
            return False
        return True

    if current_user.status in ("blocked", "inactive", "deleted"):
        logger.warning(
            "Blocked or inactive user {} update dropped silently", user_id
        )
        return False

    return True


def is_outer_middleware(
    handler_obj: Any, callback_name: str, handler_name: str
) -> bool:
    return (
        handler_obj is None
        or callback_name == "_listen_update"
        or handler_name == "_wrapped"
    )


async def handle_outer_stranger_or_dispatch(
    handler: Callable[[TelegramObject, dict[str, Any]], Any],
    event: TelegramObject,
    data: dict[str, Any],
    current_user: User | None,
    user_id: int,
    limiter: Any,
) -> Any:
    if current_user is None:
        is_public_candidate = False
        if isinstance(event, Message) and event.text and event.text.startswith("/start"):
            is_public_candidate = True
        elif isinstance(event, CallbackQuery) and event.data and event.data.startswith("req_access"):
            is_public_candidate = True

        if not is_public_candidate:
            if limiter.can_reply_to_stranger(user_id):
                from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

                from hunter.notify.messages_ru import REQ_ACCESS_BTN, STRANGER_FORBIDDEN

                kb = InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text=REQ_ACCESS_BTN, callback_data="req_access")]]
                )
                if isinstance(event, Message):
                    await event.reply(STRANGER_FORBIDDEN, reply_markup=kb)
                elif isinstance(event, CallbackQuery):
                    await event.answer(STRANGER_FORBIDDEN, show_alert=True)
            return None
    try:
        return await handler(event, data)
    except TelegramBadRequest as exc:
        if "entity" in str(exc).lower() or "parse" in str(exc).lower():
            logger.error("Caught Telegram entity parse error in outer handler: {}", exc)
            return None
        raise


async def handle_inner_stranger(
    handler: Callable[[TelegramObject, dict[str, Any]], Any],
    event: TelegramObject,
    data: dict[str, Any],
    user_id: int,
    required_role: str,
    limiter: Any,
) -> Any:
    if required_role == "public":
        try:
            return await handler(event, data)
        except TelegramBadRequest as exc:
            if "entity" in str(exc).lower() or "parse" in str(exc).lower():
                logger.error("Caught Telegram entity parse error in public handler: {}", exc)
                return None
            raise

    if limiter.can_reply_to_stranger(user_id):
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        from hunter.notify.messages_ru import REQ_ACCESS_BTN, STRANGER_FORBIDDEN

        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=REQ_ACCESS_BTN, callback_data="req_access")]]
        )
        if isinstance(event, Message):
            await event.reply(STRANGER_FORBIDDEN, reply_markup=kb)
        elif isinstance(event, CallbackQuery):
            await event.answer(STRANGER_FORBIDDEN, show_alert=True)
    return None


async def check_registered_user_role(
    current_user: User,
    required_role: str,
    user_id: int,
    callback_name: str,
    event: TelegramObject,
) -> bool:
    if required_role == "owner" and current_user.role != "owner":
        logger.warning(
            "User {} tried to access owner-only handler {}",
            user_id,
            callback_name,
        )
        from hunter.notify.messages_ru import ACCESS_DENIED

        if isinstance(event, Message):
            await event.reply(ACCESS_DENIED)
        elif isinstance(event, CallbackQuery):
            await event.answer(ACCESS_DENIED, show_alert=True)
        return False
    return True


async def safe_dispatch_handler(
    handler: Callable[[TelegramObject, dict[str, Any]], Any],
    event: TelegramObject,
    data: dict[str, Any],
    callback_name: str,
) -> Any:
    try:
        return await handler(event, data)
    except TelegramBadRequest as exc:
        if "entity" in str(exc).lower() or "parse" in str(exc).lower():
            logger.error(
                "Caught Telegram entity parse error in handler {}: {}",
                callback_name,
                exc,
            )
            return None
        raise

