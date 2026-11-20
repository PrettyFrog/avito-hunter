"""Helper functions for cb_add_confirm_launch and cb_stale_keep (slice 194)."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from loguru import logger

from hunter.core.config import get_settings
from hunter.core.product import ProductProfile, delete_product_profile, save_product_profile
from hunter.core.models import User, now_utc
from hunter.normalize.user_input import extract_avito_search_url, parse_price_input
from hunter.notify.messages_ru import (
    ADD_CANCEL_BTN,
    ADD_INVALID_NAME,
    ADD_INVALID_PRICE,
    ADD_INVALID_URL,
    ADD_STEP_2_FAQ_BTN,
    ADD_STEP_2_URL,
    ADD_STEP_3_PRICE,
    ADD_STEP_4_LOOSE_BTN,
    ADD_STEP_4_POLICY,
    ADD_STEP_4_STRICT_BTN,
    ADD_SUCCESS_TEMPLATE,
    BUDGET_OVERLOAD_OWNER_ALERT_TEMPLATE,
    BUDGET_OVERLOAD_USER,
    MY_PRODUCT_ALREADY_DELETED,
    STALE_CONFIRMED_TEMPLATE,
)
from hunter.notify.security import get_owned_product


def prepare_new_product_profile(prod_data: dict[str, Any], current_user: User) -> ProductProfile:
    profile = ProductProfile(**prod_data)
    user_min_interval = getattr(current_user, "min_interval_minutes", 15) or 15
    if user_min_interval <= 10:
        if profile.poll_minutes is None or profile.poll_minutes > user_min_interval:
            profile.poll_minutes = user_min_interval
    if profile.poll_minutes is not None and profile.poll_minutes < user_min_interval:
        profile.poll_minutes = user_min_interval
    now_iso = now_utc().isoformat()
    if not profile.created_at:
        profile.created_at = now_iso
    if not profile.last_confirmed_at:
        profile.last_confirmed_at = now_iso
    return profile


async def alert_budget_overload(
    callback: CallbackQuery,
    db_inst: Any,
    forecast: float,
    cap_80: float,
) -> None:
    if callback.message:
        await callback.message.edit_text(BUDGET_OVERLOAD_USER)
    await callback.answer("Лимит нагрузки превышен", show_alert=True)
    owner_row = await db_inst.get_owner_user()
    if owner_row and owner_row.get("chat_id") and callback.bot:
        daily_limit = int(round(cap_80 / 0.8))
        alert_text = BUDGET_OVERLOAD_OWNER_ALERT_TEMPLATE.format(
            forecast=int(round(forecast)),
            daily_limit=daily_limit,
        )
        try:
            await callback.bot.send_message(chat_id=owner_row["chat_id"], text=alert_text)
        except Exception as e:
            logger.warning("Failed to alert owner on budget overload: {}", e)


def save_profile_and_copy_photos(
    profile: ProductProfile,
    photos: list[str],
    store: Any,
) -> None:
    settings = get_settings()
    prods_dir = getattr(store, "directory", settings.data_dir / "products")
    save_product_profile(profile, directory=prods_dir)
    if photos:
        ref_dir = prods_dir / profile.id / "ref"
        ref_dir.mkdir(parents=True, exist_ok=True)
        for ph in photos:
            p_src = Path(ph)
            if p_src.exists():
                shutil.copy2(p_src, ref_dir / p_src.name)


async def handle_add_confirm_launch_callback(
    callback: CallbackQuery,
    current_user: User,
    db_inst: Any,
    store: Any,
) -> None:
    chat_id = callback.message.chat.id if callback.message else current_user.chat_id
    state = await db_inst.get_pending_product(chat_id)
    if not state or state.get("step") != "await_add_confirm" or "product" not in state.get("payload", {}):
        await callback.answer("Сессия добавления истекла.", show_alert=True)
        return
    payload = state["payload"]
    try:
        profile = prepare_new_product_profile(payload["product"], current_user)
        save_profile_and_copy_photos(profile, payload.get("photos", []), store)
        await db_inst.delete_pending_product(chat_id)
        poll_interval = profile.poll_minutes or 30
        succ_text = ADD_SUCCESS_TEMPLATE.format(name=profile.name, interval=poll_interval)
        if callback.message:
            await callback.message.edit_text(succ_text)
        await callback.answer("Запущено!")
    except Exception as exc:
        logger.exception("Failed to launch product: {}", exc)
        await callback.answer(f"Ошибка сохранения: {exc}", show_alert=True)


async def handle_stale_keep_callback(
    callback: CallbackQuery,
    current_user: User,
    store: Any,
) -> None:
    pid = (callback.data or "").split(":", 1)[1]
    p = get_owned_product(current_user, pid, store)
    if not p:
        await callback.answer(MY_PRODUCT_ALREADY_DELETED, show_alert=True)
        return
    p.last_confirmed_at = now_utc().isoformat()
    p.stale_prompted_at = None
    settings = get_settings()
    save_product_profile(p, directory=getattr(store, "directory", settings.data_dir / "products"))
    if callback.message:
        await callback.message.edit_text(STALE_CONFIRMED_TEMPLATE.format(name=p.name))
    await callback.answer("Актуальность подтверждена!")


async def handle_stale_del_callback(
    callback: CallbackQuery,
    current_user: User,
    store: Any,
) -> None:
    pid = (callback.data or "").split(":", 1)[1]
    p = get_owned_product(current_user, pid, store)
    if not p:
        await callback.answer(MY_PRODUCT_ALREADY_DELETED, show_alert=True)
        return
    delete_product_profile(pid)
    if callback.message:
        await callback.message.edit_text(f"Товар «{p.name}» удалён.")
    await callback.answer("Товар удалён")


async def handle_dialog_early_steps(
    step: str,
    message: Message,
    chat_id: int,
    user_text: str,
    payload: dict[str, Any],
    db_inst: Any,
    user_id: int,
) -> bool:
    if step == "await_add_name":
        if not (1 <= len(user_text) <= 80):
            await message.reply(ADD_INVALID_NAME)
            return True
        payload["name"] = user_text
        await db_inst.save_pending_product(chat_id, "await_add_url", payload, ttl_hours=24, owner_user_id=user_id)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=ADD_STEP_2_FAQ_BTN, callback_data="add_faq_url")],
                [InlineKeyboardButton(text=ADD_CANCEL_BTN, callback_data="add_cancel")],
            ]
        )
        await message.reply(ADD_STEP_2_URL, reply_markup=kb)
        return True

    if step == "await_add_url":
        clean_url, err_msg = extract_avito_search_url(user_text)
        if not clean_url:
            await message.reply(err_msg or ADD_INVALID_URL)
            return True
        payload["search_url"] = clean_url
        await db_inst.save_pending_product(chat_id, "await_add_price", payload, ttl_hours=24, owner_user_id=user_id)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=ADD_CANCEL_BTN, callback_data="add_cancel")]]
        )
        await message.reply(ADD_STEP_3_PRICE, reply_markup=kb)
        return True

    if step == "await_add_price":
        try:
            price = parse_price_input(user_text)
        except ValueError:
            await message.reply(ADD_INVALID_PRICE)
            return True

        payload["price"] = price
        await db_inst.save_pending_product(chat_id, "await_add_policy", payload, ttl_hours=24, owner_user_id=user_id)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=ADD_STEP_4_STRICT_BTN, callback_data="add_policy:strict")],
                [InlineKeyboardButton(text=ADD_STEP_4_LOOSE_BTN, callback_data="add_policy:loose")],
                [InlineKeyboardButton(text=ADD_CANCEL_BTN, callback_data="add_cancel")],
            ]
        )
        await message.reply(ADD_STEP_4_POLICY, reply_markup=kb)
        return True

    return False

