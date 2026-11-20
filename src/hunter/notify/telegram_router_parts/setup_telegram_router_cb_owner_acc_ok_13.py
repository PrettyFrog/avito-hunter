"""Telegram router parts: cb_owner_acc_ok and cb_owner_acc_deny helpers (T-357)."""

from __future__ import annotations

from typing import Any

from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from loguru import logger

from hunter.core.config import TierLimitConfig, get_settings
from hunter.notify.messages_ru import (
    KNOWN_USER_GREETING_TEMPLATE,
    ONBOARDING_FINISH_BTN,
    ONBOARDING_NEXT_BTN,
    ONBOARDING_SCREEN_1_TEMPLATE,
    ONBOARDING_SCREEN_2,
    ONBOARDING_SCREEN_3,
    REQ_ACCESS_APPROVED_USER,
    REQ_ACCESS_REJECTED_USER,
    clean_name,
)
from hunter.notify.telegram_router_parts.originalproductstore_01 import main_menu_keyboard


async def process_owner_access_approval(
    callback: CallbackQuery,
    db_inst: Any,
    current_user: Any,
    tier: str,
    req_id: int,
) -> None:
    """Process owner approval of an access request, creating or activating user and notifying."""
    req = await db_inst.get_access_request(req_id)
    if not req:
        await callback.answer("Заявка не найдена.", show_alert=True)
        return

    resolved = await db_inst.resolve_access_request_atomic(req_id, "approved", current_user.id)
    if not resolved:
        await callback.answer("Заявка уже была обработана ранее.", show_alert=True)
        return

    target_tg_id = req["telegram_user_id"]
    existing = await db_inst.get_user_by_telegram_id(target_tg_id)
    settings = get_settings()
    tier_cfg = settings.multiuser.tiers.get(tier, TierLimitConfig())
    if not existing:
        new_uid = await db_inst.create_user(
            telegram_user_id=target_tg_id,
            chat_id=target_tg_id,
            username=req.get("username"),
            display_name=req.get("display_name"),
            role="user",
            status="active",
            tier=tier,
            max_products=tier_cfg.max_products,
            min_interval_minutes=tier_cfg.min_interval_minutes,
            max_alerts_per_hour=tier_cfg.max_alerts_per_hour,
            invited_by=current_user.id,
        )
        await db_inst.log_user_event(new_uid, "access_approved", {"req_id": req_id, "tier": tier})
    else:
        await db_inst.set_user_status(existing["id"], "active")

    tier_ru = "Семья" if tier == "family" else "Друг"
    display_name = clean_name(req.get("display_name"))
    await callback.answer(f"Доступ разрешён ({tier_ru})", show_alert=False)
    if callback.message:
        await callback.message.edit_text(
            f"Запрос от {display_name} одобрен ({tier_ru}).",
            parse_mode="HTML",
        )

    await _notify_approved_user(callback.bot, target_tg_id, display_name, is_new=(existing is None))


async def _notify_approved_user(
    bot: Any,
    target_tg_id: int,
    display_name: str,
    is_new: bool,
) -> None:
    """Send onboarding screen 1 for new user, or access approved notice for existing."""
    if not bot:
        return
    try:
        if is_new:
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text=ONBOARDING_NEXT_BTN, callback_data="onb:2")]
                ]
            )
            text = ONBOARDING_SCREEN_1_TEMPLATE.format(name=display_name)
            await bot.send_message(
                chat_id=target_tg_id,
                text=text,
                reply_markup=kb,
                parse_mode="HTML",
            )
        else:
            await bot.send_message(
                chat_id=target_tg_id,
                text=REQ_ACCESS_APPROVED_USER,
                parse_mode="HTML",
            )
    except Exception as e:
        logger.warning("Failed to notify user {} about approval: {}", target_tg_id, e)


async def process_owner_access_denial(
    callback: CallbackQuery,
    db_inst: Any,
    current_user: Any,
    req_id: int,
) -> None:
    """Process owner denial of an access request and notify user."""
    req = await db_inst.get_access_request(req_id)
    if not req:
        await callback.answer("Заявка не найдена.", show_alert=True)
        return

    resolved = await db_inst.resolve_access_request_atomic(req_id, "rejected", current_user.id)
    if not resolved:
        await callback.answer("Заявка уже была обработана ранее.", show_alert=True)
        return

    display_name = clean_name(req.get("display_name"))
    await callback.answer("Запрос отклонён", show_alert=False)
    if callback.message:
        await callback.message.edit_text(
            f"Запрос от {display_name} отклонён.",
            parse_mode="HTML",
        )

    target_tg_id = req["telegram_user_id"]
    if callback.bot:
        try:
            await callback.bot.send_message(
                chat_id=target_tg_id,
                text=REQ_ACCESS_REJECTED_USER,
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning("Failed to notify user {} about rejection: {}", target_tg_id, e)


async def handle_onboarding_step_2(callback: CallbackQuery) -> None:
    """Show onboarding screen 2."""
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=ONBOARDING_NEXT_BTN, callback_data="onb:3")]]
    )
    if callback.message:
        await callback.message.edit_text(ONBOARDING_SCREEN_2, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


async def handle_onboarding_step_3(callback: CallbackQuery) -> None:
    """Show onboarding screen 3."""
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=ONBOARDING_FINISH_BTN, callback_data="onb:done")]
        ]
    )
    if callback.message:
        await callback.message.edit_text(ONBOARDING_SCREEN_3, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


async def handle_onboarding_done(callback: CallbackQuery, current_user: Any) -> None:
    """Finish onboarding, delete message, send greeting with main menu."""
    name = clean_name(current_user.display_name if current_user else "")
    if callback.message:
        await callback.message.delete()
    if callback.message and callback.message.bot:
        await callback.message.bot.send_message(
            chat_id=callback.from_user.id,
            text=KNOWN_USER_GREETING_TEMPLATE.format(name=name),
            reply_markup=main_menu_keyboard(),
            parse_mode="HTML",
        )
    await callback.answer()
