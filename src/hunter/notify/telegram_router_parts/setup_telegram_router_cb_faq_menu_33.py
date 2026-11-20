"""Telegram router parts: cb_faq_menu, cb_faq_item, cmd_quiet, cmd_guide, cmd_support (T-361)."""

from __future__ import annotations

import html
import re
from typing import Any

from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from hunter.notify.broadcast import send_broadcast
from hunter.notify.messages_ru import (
    BROADCAST_CANCELLED,
    BROADCAST_PROMPT,
    BROADCAST_RESULT_TEMPLATE,
    FAQ_BACK_BTN,
    FAQ_ITEMS,
    FAQ_MENU_TITLE,
    GUIDE_TEXT,
    QUIET_HOURS_DISABLED,
    QUIET_HOURS_INVALID,
    QUIET_HOURS_SET_TEMPLATE,
    QUIET_HOURS_USAGE,
    SUPPORT_BTN_REPLY,
    SUPPORT_LIMIT_REACHED,
    SUPPORT_PROMPT,
    SUPPORT_REPLY_PROMPT_TEMPLATE,
    SUPPORT_TICKETS_EMPTY,
    SUPPORT_TICKETS_HEADER,
)
from hunter.notify.telegram_router_parts.originalproductstore_01 import _BROADCAST_PAYLOADS


async def handle_faq_menu_callback(callback: CallbackQuery) -> None:
    """Render FAQ menu with item buttons."""
    kb_rows = []
    for key, (q_text, _) in FAQ_ITEMS.items():
        kb_rows.append([InlineKeyboardButton(text=q_text, callback_data=f"faq:{key}")])
    if callback.message:
        await callback.message.edit_text(
            FAQ_MENU_TITLE,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
            parse_mode="HTML",
        )
    await callback.answer()


async def handle_faq_item_callback(callback: CallbackQuery) -> None:
    """Render single FAQ item with back button."""
    key = (callback.data or "").split(":", 1)[1]
    if key not in FAQ_ITEMS:
        await callback.answer()
        return
    q_text, a_text = FAQ_ITEMS[key]
    text = f"<b>{q_text}</b>\n\n{a_text}"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=FAQ_BACK_BTN, callback_data="faq_menu")]]
    )
    if callback.message:
        await callback.message.edit_text(
            text,
            reply_markup=kb,
            parse_mode="HTML",
        )
    await callback.answer()


async def handle_quiet_command(
    message: Message,
    current_user: Any,
    database: Any,
) -> None:
    """Handle /quiet command: status, off, or setting start-end interval."""
    text = (message.text or "").strip()
    parts = text.split()
    if len(parts) == 1:
        if current_user.quiet_start is not None and current_user.quiet_end is not None:
            await message.reply(
                f"Сейчас тихие часы: с {current_user.quiet_start}:00 "
                f"до {current_user.quiet_end}:00 (МСК).\n"
                f"Чтобы изменить: /quiet 23-8\nЧтобы выключить: /quiet off"
            )
        else:
            await message.reply(QUIET_HOURS_USAGE)
        return

    arg = parts[1].strip().lower()
    if arg == "off":
        await database.update_user_quiet_hours(current_user.id, None, None)
        current_user.quiet_start = None
        current_user.quiet_end = None
        await message.reply(QUIET_HOURS_DISABLED)
        return

    m = re.match(r"^(\d{1,2})(?::00)?-(\d{1,2})(?::00)?$", arg)
    if not m:
        await message.reply(QUIET_HOURS_INVALID)
        return

    start_h = int(m.group(1))
    end_h = int(m.group(2))
    if not (0 <= start_h <= 23 and 0 <= end_h <= 23):
        await message.reply(QUIET_HOURS_INVALID)
        return

    await database.update_user_quiet_hours(current_user.id, start_h, end_h)
    current_user.quiet_start = start_h
    current_user.quiet_end = end_h
    await message.reply(QUIET_HOURS_SET_TEMPLATE.format(start=start_h, end=end_h))


async def handle_guide_command(message: Message, safe_reply_fn: Any) -> None:
    """Handle /guide command."""
    await safe_reply_fn(message, GUIDE_TEXT, parse_mode="HTML")


async def handle_support_command(
    message: Message,
    current_user: Any,
    db_inst: Any,
    safe_reply_fn: Any,
) -> None:
    """Handle /support command: check rate limit and await user question."""
    cnt = await db_inst.get_user_support_tickets_count_24h(current_user.id)
    if cnt >= 3:
        await safe_reply_fn(message, SUPPORT_LIMIT_REACHED)
        return
    await db_inst.save_pending_product(message.chat.id, "await_support_text", {}, ttl_hours=2)
    await safe_reply_fn(message, SUPPORT_PROMPT)


async def handle_tickets_command(
    message: Message,
    db_inst: Any,
    safe_reply_fn: Any,
) -> None:
    """Handle /tickets command showing open support tickets."""
    tickets = await db_inst.get_open_support_tickets()
    if not tickets:
        await safe_reply_fn(message, SUPPORT_TICKETS_EMPTY)
        return
    await safe_reply_fn(message, SUPPORT_TICKETS_HEADER, parse_mode="HTML")
    for t in tickets[:10]:
        u = await db_inst.get_user_by_id(t["user_id"])
        user_repr = f"@{u['username']}" if u and u.get("username") else f"ID {t['user_id']}"
        t_text = (
            f"🎫 <b>Обращение #{t['id']}</b>\n"
            f"От: {user_repr}\n"
            f"Дата: {t['created_at'][:19].replace('T', ' ')}\n\n"
            f"{html.escape(t['message_text'])}"
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=SUPPORT_BTN_REPLY, callback_data=f"ticket_reply:{t['id']}"
                    )
                ]
            ]
        )
        await safe_reply_fn(message, t_text, reply_markup=kb, parse_mode="HTML")


async def handle_ticket_reply_callback(
    query: CallbackQuery,
    current_user: Any,
    db_inst: Any,
) -> None:
    """Handle ticket_reply:<id> callback: prompt owner for reply text."""
    parts = (query.data or "").split(":", 1)
    if len(parts) < 2:
        await query.answer("Неверный callback", show_alert=True)
        return
    ticket_id = int(parts[1])
    ticket = await db_inst.get_support_ticket(ticket_id)
    if not ticket or ticket.get("status") != "open":
        await query.answer("Обращение уже закрыто или не найдено.", show_alert=True)
        return
    chat_id = query.message.chat.id if query.message else current_user.chat_id
    await db_inst.save_pending_product(
        chat_id, "await_ticket_reply", {"ticket_id": ticket_id}, ttl_hours=2
    )
    await query.answer()
    if query.message:
        await query.message.reply(SUPPORT_REPLY_PROMPT_TEMPLATE.format(ticket_id=ticket_id))


async def handle_broadcast_command(
    message: Message,
    db_inst: Any,
    safe_reply_fn: Any,
) -> None:
    """Handle /broadcast command: save pending text state and show prompt."""
    await db_inst.save_pending_product(message.chat.id, "await_broadcast_text", {}, ttl_hours=2)
    await safe_reply_fn(message, BROADCAST_PROMPT)


async def handle_broadcast_send_callback(
    query: CallbackQuery,
    current_user: Any,
    db_inst: Any,
    default_bot: Any = None,
) -> None:
    """Handle bc:send:<audience> callback executing broadcast."""
    audience = (query.data or "").split(":", 2)[2]
    chat_id = query.message.chat.id if query.message else current_user.chat_id
    text = _BROADCAST_PAYLOADS.pop(chat_id, None)
    if not text:
        await query.answer("Текст рассылки не найден или устарел.", show_alert=True)
        return
    await db_inst.delete_pending_product(chat_id)
    await query.answer("Запускаю рассылку...")
    bot = query.bot or default_bot
    delivered, failed = await send_broadcast(
        db=db_inst,
        text=text,
        target_audience=audience,
        bot=bot,
        sender_user_id=current_user.id,
    )
    result_text = BROADCAST_RESULT_TEMPLATE.format(
        delivered=delivered,
        failed=failed,
    )
    if query.message:
        await query.message.edit_text(result_text, parse_mode="HTML")


async def handle_broadcast_cancel_callback(
    query: CallbackQuery,
    current_user: Any,
    db_inst: Any,
) -> None:
    """Handle bc:cancel callback clearing broadcast payload."""
    chat_id = query.message.chat.id if query.message else current_user.chat_id
    _BROADCAST_PAYLOADS.pop(chat_id, None)
    await db_inst.delete_pending_product(chat_id)
    await query.answer("Рассылка отменена.")
    if query.message:
        await query.message.edit_text(BROADCAST_CANCELLED)
