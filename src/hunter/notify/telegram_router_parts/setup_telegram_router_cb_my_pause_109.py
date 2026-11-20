"""Telegram router parts: my product pause, resume, price, photo handlers (T-371)."""

from __future__ import annotations

from typing import Any

from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from hunter.core.config import get_settings
from hunter.core.models import now_utc
from hunter.core.product import (
    apply_product_pause,
    clear_product_pause,
    delete_product_profile,
    save_product_profile,
)
from hunter.notify.messages_ru import (
    ADD_CANCEL_BTN,
    MY_DELETE_CANCEL_BTN,
    MY_DELETE_CONFIRM_BTN,
    MY_DELETE_CONFIRM_TEMPLATE,
    MY_PHOTO_PROMPT,
    MY_PRICE_EDIT_PROMPT,
    MY_PRODUCT_ALREADY_DELETED,
)
from hunter.notify.security import get_owned_product


async def handle_my_pause_callback(
    callback: CallbackQuery,
    current_user: Any,
    store: Any,
    db_inst: Any,
    format_product_card_fn: Any,
    product_card_keyboard_fn: Any,
) -> None:
    """Handle my_pause: callback pausing user product."""
    pid = (callback.data or "").split(":", 1)[1]
    p = get_owned_product(current_user, pid, store)
    if not p:
        await callback.answer(MY_PRODUCT_ALREADY_DELETED, show_alert=True)
        return
    apply_product_pause(p, "user_paused", actor_role=current_user.role)
    settings = get_settings()
    save_product_profile(p, directory=getattr(store, "directory", settings.data_dir / "products"))
    card_text = await format_product_card_fn(p, db_inst)
    kb = product_card_keyboard_fn(p)
    if callback.message:
        try:
            await callback.message.edit_text(card_text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            pass
    await callback.answer("Поиск поставлен на паузу.")


async def handle_my_resume_callback(
    callback: CallbackQuery,
    current_user: Any,
    store: Any,
    db_inst: Any,
    format_product_card_fn: Any,
    product_card_keyboard_fn: Any,
) -> None:
    """Handle my_resume: callback resuming user product."""
    pid = (callback.data or "").split(":", 1)[1]
    p = get_owned_product(current_user, pid, store)
    if not p:
        await callback.answer(MY_PRODUCT_ALREADY_DELETED, show_alert=True)
        return
    ok, err_msg = clear_product_pause(p, actor_role=current_user.role)
    if not ok:
        await callback.answer(err_msg, show_alert=True)
        return
    p.last_confirmed_at = now_utc().isoformat()
    settings = get_settings()
    save_product_profile(p, directory=getattr(store, "directory", settings.data_dir / "products"))
    card_text = await format_product_card_fn(p, db_inst)
    kb = product_card_keyboard_fn(p)
    if callback.message:
        try:
            await callback.message.edit_text(card_text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            pass
    await callback.answer("Поиск возобновлён.")


async def handle_my_price_callback(
    callback: CallbackQuery,
    current_user: Any,
    store: Any,
    db_inst: Any,
) -> None:
    """Handle my_price: callback prompting new target price."""
    pid = (callback.data or "").split(":", 1)[1]
    p = get_owned_product(current_user, pid, store)
    if not p:
        await callback.answer(MY_PRODUCT_ALREADY_DELETED, show_alert=True)
        return
    chat_id = callback.message.chat.id if callback.message else current_user.chat_id
    await db_inst.save_pending_product(
        chat_id, "await_my_price", {"product_id": pid}, ttl_hours=24, owner_user_id=current_user.id
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=ADD_CANCEL_BTN, callback_data="add_cancel")]]
    )
    if callback.message:
        await callback.message.reply(MY_PRICE_EDIT_PROMPT.format(name=p.name), reply_markup=kb)
    await callback.answer()


async def handle_my_photo_callback(
    callback: CallbackQuery,
    current_user: Any,
    store: Any,
    db_inst: Any,
) -> None:
    """Handle my_photo: callback prompting reference photo."""
    pid = (callback.data or "").split(":", 1)[1]
    p = get_owned_product(current_user, pid, store)
    if not p:
        await callback.answer(MY_PRODUCT_ALREADY_DELETED, show_alert=True)
        return
    chat_id = callback.message.chat.id if callback.message else current_user.chat_id
    await db_inst.save_pending_product(
        chat_id, "await_my_photo", {"product_id": pid}, ttl_hours=24, owner_user_id=current_user.id
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=ADD_CANCEL_BTN, callback_data="add_cancel")]]
    )
    if callback.message:
        await callback.message.reply(MY_PHOTO_PROMPT.format(name=p.name), reply_markup=kb)
    await callback.answer()


async def handle_my_findings_callback(
    callback: CallbackQuery,
    current_user: Any,
    store: Any,
    db_inst: Any,
    get_findings_fn: Any,
) -> None:
    """Handle my_findings: callback displaying recent findings for a product."""
    pid = (callback.data or "").split(":", 1)[1]
    p = get_owned_product(current_user, pid, store)
    if not p:
        await callback.answer(MY_PRODUCT_ALREADY_DELETED, show_alert=True)
        return
    findings = await get_findings_fn(db_inst, pid, limit=5)
    if not findings:
        await callback.answer("Пока ничего не найдено по этому товару.", show_alert=True)
        return
    lines = [f"📋 <b>Последние находки по «{p.name}»:</b>\n"]
    for f in findings:
        pr_val = f"{f['price']:,} ₽".replace(",", " ") if f.get("price") else "цена не указана"
        url = f.get("url") or "https://www.avito.ru"
        lines.append(f'• <a href="{url}">{f["title"]}</a> — {pr_val} ({f["time_ago"]})')
    if callback.message:
        await callback.message.reply(
            "\n".join(lines), parse_mode="HTML", disable_web_page_preview=True
        )
    await callback.answer()


async def handle_my_del_ask_callback(
    callback: CallbackQuery,
    current_user: Any,
    store: Any,
) -> None:
    """Handle my_del_ask: callback showing product delete confirmation prompt."""
    pid = (callback.data or "").split(":", 1)[1]
    p = get_owned_product(current_user, pid, store)
    if not p:
        await callback.answer(MY_PRODUCT_ALREADY_DELETED, show_alert=True)
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=MY_DELETE_CONFIRM_BTN, callback_data=f"my_del_do:{pid}")],
            [InlineKeyboardButton(text=MY_DELETE_CANCEL_BTN, callback_data=f"my_del_cancel:{pid}")],
        ]
    )
    if callback.message:
        await callback.message.edit_text(
            MY_DELETE_CONFIRM_TEMPLATE.format(name=p.name), reply_markup=kb
        )
    await callback.answer()


async def handle_my_del_cancel_callback(
    callback: CallbackQuery,
    current_user: Any,
    store: Any,
    db_inst: Any,
    format_product_card_fn: Any,
    product_card_keyboard_fn: Any,
) -> None:
    """Handle my_del_cancel: callback reverting deletion and restoring card view."""
    pid = (callback.data or "").split(":", 1)[1]
    p = get_owned_product(current_user, pid, store)
    if not p:
        await callback.answer(MY_PRODUCT_ALREADY_DELETED, show_alert=True)
        if callback.message:
            await callback.message.edit_text(MY_PRODUCT_ALREADY_DELETED)
        return
    card_text = await format_product_card_fn(p, db_inst)
    kb = product_card_keyboard_fn(p)
    if callback.message:
        await callback.message.edit_text(card_text, reply_markup=kb, parse_mode="HTML")
    await callback.answer("Удаление отменено")


async def handle_my_del_do_callback(
    callback: CallbackQuery,
    current_user: Any,
    store: Any,
) -> None:
    """Handle my_del_do: callback deleting product profile."""
    pid = (callback.data or "").split(":", 1)[1]
    p = get_owned_product(current_user, pid, store)
    if not p:
        await callback.answer(MY_PRODUCT_ALREADY_DELETED, show_alert=True)
        if callback.message:
            await callback.message.edit_text(MY_PRODUCT_ALREADY_DELETED)
        return
    if hasattr(store, "delete"):
        store.delete(pid)
    else:
        delete_product_profile(pid)
    if callback.message:
        await callback.message.edit_text(f"Товар «{p.name}» удалён.")
    await callback.answer("Удалено")
