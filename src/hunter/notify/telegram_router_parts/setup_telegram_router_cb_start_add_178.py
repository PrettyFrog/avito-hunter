"""Telegram router parts: /add callback query handlers part 1 (T-386)."""

from __future__ import annotations

from typing import Any

from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from hunter.core.models import User
from hunter.notify.messages_ru import (
    ADD_ATTEMPTS_LIMIT_REACHED,
    ADD_CANCEL_BTN,
    ADD_HOURLY_LIMIT_REACHED,
    ADD_LIMIT_REACHED_TEMPLATE,
    ADD_STEP_1_NAME,
    ADD_STEP_2_URL,
    ADD_STEP_3_PRICE,
    ADD_STEP_4_LOOSE_EXPLAIN,
    ADD_STEP_5_PHOTO,
    ADD_STEP_5_SKIP_BTN,
    ADD_STEP_6_REQUIREMENTS,
    ADD_STEP_6_SKIP_BTN,
)


async def handle_start_add_callback(
    callback: CallbackQuery,
    current_user: User,
    store: Any,
    db_inst: Any,
) -> None:
    from hunter.notify.telegram_handlers.public_add_entry import maybe_start_public_add

    if callback.message is None:
        await callback.answer("Поиск недоступен.", show_alert=True)
        return
    if await maybe_start_public_add(callback.message, current_user, db_inst):
        await callback.answer()
        return
    user_prods = store.get_for_user(current_user.id)
    if len(user_prods) >= current_user.max_products:
        await callback.answer(
            ADD_LIMIT_REACHED_TEMPLATE.format(max_count=current_user.max_products),
            show_alert=True,
        )
        return
    if current_user.role != "owner":
        u_attempts = await db_inst.count_recent_add_attempts(user_id=current_user.id, hours=24)
        if u_attempts >= 5:
            await callback.answer(ADD_ATTEMPTS_LIMIT_REACHED, show_alert=True)
            return
        g_attempts = await db_inst.count_recent_add_attempts(user_id=None, hours=1)
        if g_attempts >= 10:
            await callback.answer(ADD_HOURLY_LIMIT_REACHED, show_alert=True)
            return

    await db_inst.log_user_event(current_user.id, "add_attempt")

    chat_id = callback.message.chat.id if callback.message else current_user.chat_id
    await db_inst.save_pending_product(
        chat_id=chat_id,
        step="await_add_name",
        payload={"photos": [], "user_id": current_user.id},
        ttl_hours=24,
        owner_user_id=current_user.id,
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=ADD_CANCEL_BTN, callback_data="add_cancel")]]
    )
    if callback.message:
        await callback.message.reply(ADD_STEP_1_NAME, reply_markup=kb)
    await callback.answer()


async def handle_add_faq_url_callback(callback: CallbackQuery) -> None:
    faq_text = (
        "📌 <b>Как скопировать ссылку с Авито:</b>\n\n"
        "1. Откройте Авито в браузере или приложении.\n"
        "2. Введите название нужного товара и настройте поиск (город, состояние).\n"
        "3. Скопируйте ссылку из адресной строки браузера (например: <code>https://www.avito.ru/moskva/...</code>).\n"
        "4. Отправьте эту ссылку сюда в чат."
    )
    if callback.message:
        await callback.message.reply(faq_text, parse_mode="HTML")
    await callback.answer()


async def handle_add_policy_callback(
    callback: CallbackQuery,
    current_user: User,
    db_inst: Any,
) -> None:
    policy = (callback.data or "").split(":", 1)[1]
    chat_id = callback.message.chat.id if callback.message else current_user.chat_id
    state = await db_inst.get_pending_product(chat_id)
    if not state or state.get("step") != "await_add_policy":
        await callback.answer("Сессия добавления истекла.", show_alert=True)
        return
    payload = state["payload"]
    payload["policy"] = policy
    await db_inst.save_pending_product(
        chat_id, "await_add_photo", payload, ttl_hours=24, owner_user_id=current_user.id
    )
    text = ADD_STEP_5_PHOTO
    if policy == "loose":
        text = f"{ADD_STEP_4_LOOSE_EXPLAIN}\n\n{text}"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=ADD_STEP_5_SKIP_BTN, callback_data="add_photo_skip")],
            [InlineKeyboardButton(text=ADD_CANCEL_BTN, callback_data="add_cancel")],
        ]
    )
    if callback.message:
        await callback.message.reply(text, reply_markup=kb)
    await callback.answer()


async def handle_add_photo_proceed_callback(
    callback: CallbackQuery,
    current_user: User,
    db_inst: Any,
) -> None:
    chat_id = callback.message.chat.id if callback.message else current_user.chat_id
    state = await db_inst.get_pending_product(chat_id)
    if not state or state.get("step") != "await_add_photo":
        await callback.answer("Сессия добавления истекла.", show_alert=True)
        return
    payload = state["payload"]
    await db_inst.save_pending_product(
        chat_id, "await_add_reqs", payload, ttl_hours=24, owner_user_id=current_user.id
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=ADD_STEP_6_SKIP_BTN, callback_data="add_reqs_skip")],
            [InlineKeyboardButton(text=ADD_CANCEL_BTN, callback_data="add_cancel")],
        ]
    )
    if callback.message:
        await callback.message.reply(ADD_STEP_6_REQUIREMENTS, reply_markup=kb)
    await callback.answer()


async def handle_add_reqs_skip_callback(
    callback: CallbackQuery,
    current_user: User,
    db_inst: Any,
    start_add_verification_user_fn: Any,
) -> None:
    chat_id = callback.message.chat.id if callback.message else current_user.chat_id
    state = await db_inst.get_pending_product(chat_id)
    if not state or state.get("step") != "await_add_reqs":
        await callback.answer("Сессия добавления истекла.", show_alert=True)
        return
    payload = state["payload"]
    payload["requirements"] = ""
    await callback.answer()
    if callback.message:
        await start_add_verification_user_fn(callback.message, payload, db_inst, current_user)


async def handle_add_verify_retry_callback(
    callback: CallbackQuery,
    current_user: User,
    db_inst: Any,
    start_add_verification_user_fn: Any,
) -> None:
    chat_id = callback.message.chat.id if callback.message else current_user.chat_id
    state = await db_inst.get_pending_product(chat_id)
    if not state or state.get("step") not in (
        "await_add_verify_failed",
        "await_add_confirm",
        "await_add_reqs",
    ):
        await callback.answer("Сессия добавления истекла.", show_alert=True)
        return
    await callback.answer("Повторяем проверку ссылки...")
    if callback.message:
        await start_add_verification_user_fn(
            callback.message, state["payload"], db_inst, current_user
        )


async def handle_add_verify_edit_url_callback(
    callback: CallbackQuery,
    current_user: User,
    db_inst: Any,
) -> None:
    chat_id = callback.message.chat.id if callback.message else current_user.chat_id
    state = await db_inst.get_pending_product(chat_id)
    if not state:
        await callback.answer("Сессия добавления истекла.", show_alert=True)
        return
    await db_inst.save_pending_product(
        chat_id, "await_add_url_edit", state["payload"], ttl_hours=24, owner_user_id=current_user.id
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=ADD_CANCEL_BTN, callback_data="add_cancel")]]
    )
    if callback.message:
        await callback.message.reply(ADD_STEP_2_URL, reply_markup=kb)
    await callback.answer()


async def handle_add_edit_price_callback(
    callback: CallbackQuery,
    current_user: User,
    db_inst: Any,
) -> None:
    chat_id = callback.message.chat.id if callback.message else current_user.chat_id
    state = await db_inst.get_pending_product(chat_id)
    if not state:
        await callback.answer("Сессия добавления истекла.", show_alert=True)
        return
    await db_inst.save_pending_product(
        chat_id,
        "await_add_price_edit",
        state["payload"],
        ttl_hours=24,
        owner_user_id=current_user.id,
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=ADD_CANCEL_BTN, callback_data="add_cancel")]]
    )
    if callback.message:
        await callback.message.reply(ADD_STEP_3_PRICE, reply_markup=kb)
    await callback.answer()
