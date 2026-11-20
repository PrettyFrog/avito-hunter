"""Telegram router parts: cmd_start helpers (T-355)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from loguru import logger

from hunter.core.config import TierLimitConfig, get_settings
from hunter.core.models import now_utc
from hunter.core.product import get_product_store, save_product_profile
from hunter.notify.messages_ru import (
    BOT_RESUMED_USER_NOTICE,
    KNOWN_USER_GREETING_TEMPLATE,
    INVITE_ALREADY_USED,
    NEW_USER_OWNER_ALERT_TEMPLATE,
    ONBOARDING_NEXT_BTN,
    ONBOARDING_SCREEN_1_TEMPLATE,
    REQ_ACCESS_BTN,
    REQ_ACCESS_COOLDOWN,
    REQ_ACCESS_LIMIT_REACHED_OWNER,
    REQ_ACCESS_OWNER_NOTIFY_TEMPLATE,
    REQ_ACCESS_SENT,
    OWNER_START_GREETING,
    STRANGER_FORBIDDEN,
    clean_name,
)
from hunter.notify.security import GLOBAL_STRANGER_LIMITER
from hunter.notify.telegram_handlers.public_add import ensure_public_tenant_selector
from hunter.notify.telegram_router_parts.originalproductstore_01 import (
    check_user_blocked_status,
    main_menu_keyboard,
    parse_start_args,
    validate_start_invite,
)


async def bind_public_telegram_destination(
    service: Any, internal_user_id: int, message: Message
) -> None:
    sender = getattr(message, "from_user", None)
    chat = getattr(message, "chat", None)
    if not service or not sender or not chat or getattr(chat, "type", "private") != "private":
        return
    if int(sender.id) != int(chat.id):
        return
    await service.bind_telegram_chat(internal_user_id, chat.id)


async def handle_start_command(
    message: Message,
    command: Any,
    current_user: Any,
    resolve_db: Any,
    resolve_store: Any,
    owner_chat_id: str,
    subscription_service: Any,
) -> None:
    db_inst = resolve_db()
    await db_inst.delete_pending_product(message.chat.id)
    user_id = message.from_user.id if message.from_user else 0
    user_display = message.from_user.full_name or message.from_user.first_name if message.from_user else "Пользователь"
    username = message.from_user.username if message.from_user else None
    should_stop, current_user = await check_user_blocked_status(db_inst, message, user_id, current_user)
    if should_stop:
        return
    if (current_user and current_user.role == "owner") or str(user_id) == owner_chat_id:
        if current_user:
            await bind_public_telegram_destination(subscription_service, current_user.id, message)
        await message.reply(OWNER_START_GREETING, parse_mode="HTML")
        return
    args = parse_start_args(command, message.text)
    if args and args.startswith("inv_"):
        is_valid, invite, token_hash = await validate_start_invite(
            db_inst, message, args[4:].strip(), user_id, current_user
        )
        if not is_valid or invite is None:
            return
        claimed = await db_inst.consume_invite_atomic(token_hash, now_utc().isoformat())
        if not claimed:
            await message.reply(INVITE_ALREADY_USED, parse_mode="HTML")
            return
        await register_invite_user_and_notify(
            db_inst, message, invite, user_id, username, user_display
        )
        user_data = await db_inst.get_user_by_telegram_id(user_id)
        if user_data:
            await ensure_public_tenant_selector(
                db_inst, int(user_data["id"]), subscription_service
            )
            await bind_public_telegram_destination(
                subscription_service, int(user_data["id"]), message
            )
        return
    if current_user is not None and current_user.status == "active":
        await ensure_public_tenant_selector(db_inst, current_user.id, subscription_service)
        await bind_public_telegram_destination(subscription_service, current_user.id, message)
        await handle_known_active_user_start(message, current_user, store=resolve_store())
        return
    await handle_stranger_start(message)


async def register_invite_user_and_notify(
    db_inst: Any,
    message: Message,
    invite: dict[str, Any],
    user_id: int,
    username: str | None,
    user_display: str,
) -> None:
    """Create user record from invite, log event, notify owner, and send onboarding screen 1."""
    tier = invite.get("tier", "friend")
    settings = get_settings()
    tier_cfg = settings.multiuser.tiers.get(tier, TierLimitConfig())
    new_uid = await db_inst.create_user(
        telegram_user_id=user_id,
        chat_id=user_id,
        username=username,
        display_name=user_display,
        role="user",
        status="active",
        tier=tier,
        max_products=tier_cfg.max_products,
        min_interval_minutes=tier_cfg.min_interval_minutes,
        max_alerts_per_hour=tier_cfg.max_alerts_per_hour,
        invited_by=invite.get("created_by"),
    )
    await db_inst.log_user_event(new_uid, "invite_registered", {"invite_id": invite.get("id"), "tier": tier})

    # Notify owner
    tier_ru = "Семья" if tier == "family" else "Друг"
    owner_user = await db_inst.get_owner_user()
    if owner_user and message.bot:
        try:
            await message.bot.send_message(
                chat_id=owner_user["chat_id"],
                text=NEW_USER_OWNER_ALERT_TEMPLATE.format(
                    name=clean_name(user_display),
                    tier_ru=tier_ru,
                ),
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning("Failed to notify owner of new user: {}", e)

    # Onboarding Screen 1
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=ONBOARDING_NEXT_BTN, callback_data="onb:2")]]
    )
    await message.reply(
        ONBOARDING_SCREEN_1_TEMPLATE.format(name=clean_name(user_display)),
        reply_markup=kb,
        parse_mode="HTML",
    )


async def handle_known_active_user_start(
    message: Message,
    current_user: Any,
    store: Any = None,
) -> None:
    """Handle /start for known active user, resuming any bot_blocked products."""
    store = store or get_product_store()
    profiles, _ = store.load_all()
    settings = get_settings()
    resumed_count = 0
    for p in profiles.values():
        if p.owner_user_id == current_user.id and p.paused_reason == "bot_blocked":
            p.mode = "live"
            p.paused_reason = None
            save_product_profile(p, directory=getattr(store, "directory", settings.data_dir / "products"))
            resumed_count += 1

    reply_text = KNOWN_USER_GREETING_TEMPLATE.format(name=clean_name(current_user.display_name))
    if resumed_count > 0:
        reply_text += f"\n\n{BOT_RESUMED_USER_NOTICE}"
    await message.reply(
        reply_text,
        reply_markup=main_menu_keyboard(),
        parse_mode="HTML",
    )


async def handle_stranger_start(message: Message) -> None:
    """Handle /start for stranger without valid invite."""
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=REQ_ACCESS_BTN, callback_data="req_access")]]
    )
    await message.reply(STRANGER_FORBIDDEN, reply_markup=kb, parse_mode="HTML")


async def validate_and_gate_access_request(
    db_inst: Any,
    callback: CallbackQuery,
    user_id: int,
    current_user: Any,
) -> bool:
    """Check if stranger can submit access request. Returns True if can proceed."""
    user_data = await db_inst.get_user_by_telegram_id(user_id)
    if user_data and user_data.get("status") in ("blocked", "inactive"):
        return False

    if current_user is not None and current_user.status == "active":
        await callback.answer("Вы уже зарегистрированы в боте.", show_alert=True)
        return False

    rejected_count = await db_inst.get_rejected_access_requests_count(user_id)
    if rejected_count >= 3:
        return False

    recent_time = await db_inst.get_recent_access_request_time(user_id)
    if recent_time:
        dt_recent = datetime.fromisoformat(recent_time)
        if dt_recent.tzinfo is None:
            dt_recent = dt_recent.replace(tzinfo=UTC)
        if (now_utc() - dt_recent).total_seconds() < 86400:
            await callback.answer(REQ_ACCESS_COOLDOWN, show_alert=True)
            return False

    pending_count = await db_inst.get_pending_access_requests_count()
    rate_ok = GLOBAL_STRANGER_LIMITER.check_access_request_rate()
    if pending_count >= 20 or not rate_ok:
        if GLOBAL_STRANGER_LIMITER.should_alert_owner_access_flood():
            owner_user = await db_inst.get_owner_user()
            if owner_user and callback.bot:
                try:
                    await callback.bot.send_message(
                        chat_id=owner_user["chat_id"],
                        text=REQ_ACCESS_LIMIT_REACHED_OWNER,
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
        await callback.answer(REQ_ACCESS_SENT, show_alert=True)
        return False

    return True


async def create_access_request_and_notify_owner(
    db_inst: Any,
    callback: CallbackQuery,
    user_id: int,
    username: str | None,
    user_display: str,
) -> None:
    """Create access request in DB, answer callback, and notify owner."""
    req_id = await db_inst.create_access_request(user_id, username, user_display)
    await callback.answer(REQ_ACCESS_SENT, show_alert=True)

    owner_user = await db_inst.get_owner_user()
    if owner_user and callback.bot:
        user_repr = f"@{username}" if username else f"ID {user_id}"
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="Разрешить (друг)", callback_data=f"acc_ok:friend:{req_id}"),
                    InlineKeyboardButton(text="Разрешить (семья)", callback_data=f"acc_ok:family:{req_id}"),
                ],
                [
                    InlineKeyboardButton(text="Отклонить", callback_data=f"acc_deny:{req_id}"),
                ],
            ]
        )
        try:
            await callback.bot.send_message(
                chat_id=owner_user["chat_id"],
                text=REQ_ACCESS_OWNER_NOTIFY_TEMPLATE.format(
                    name=clean_name(user_display),
                    user_repr=clean_name(user_repr),
                ),
                reply_markup=kb,
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning("Failed to notify owner about access request: {}", e)
