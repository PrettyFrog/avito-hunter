"""Telegram router parts: format errors report helpers (T-369)."""

from __future__ import annotations

import html
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from hunter.core.config import TierLimitConfig, get_settings
from hunter.core.models import now_utc
from hunter.notify.messages_ru import ADMIN_BTN_BACK, MY_LIST_EMPTY


def format_empty_errors_report(since_hours: int) -> tuple[str, InlineKeyboardMarkup]:
    """Format report when zero network errors were recorded."""
    text = (
        f"✅ <b>Лог сетевых ошибок за последние {since_hours}ч:</b>\n\n"
        'Сетевых ошибок (502, таймаутов, 429/403) не зафиксировано. Все запросы к '
            'Авито отработали штатно.'
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔄 Обновить", callback_data="admin:errors:0"),
                InlineKeyboardButton(text=ADMIN_BTN_BACK, callback_data="admin:root"),
            ]
        ]
    )
    return text, kb


def format_error_event_lines(page_events: list[dict[str, Any]]) -> str:
    """Format single page of error events into html strings."""
    tz_msk = ZoneInfo("Europe/Moscow")
    now_msk = now_utc().astimezone(tz_msk)

    event_lines = []
    for ev in page_events:
        ts_str = ev.get("ts") or ""
        try:
            ev_dt = datetime.fromisoformat(ts_str)
            if ev_dt.tzinfo is None:
                ev_dt = ev_dt.replace(tzinfo=UTC)
            ev_msk = ev_dt.astimezone(tz_msk)
            if ev_msk.date() == now_msk.date():
                time_label = ev_msk.strftime("%H:%M:%S")
            else:
                time_label = ev_msk.strftime("%d.%m %H:%M")
        except Exception:
            time_label = ts_str[:19].replace("T", " ")

        stat = ev.get("status")
        err = ev.get("error")
        if stat and stat > 0:
            err_badge = f"HTTP {stat}"
        elif err:
            err_badge = err.split(":")[0]
        else:
            err_badge = "Ошибка"

        pid = ev.get("product_id") or "—"
        rtype = ev.get("req_type") or ""
        rtype_ru = "поиск" if "search" in rtype else ("карточка" if "card" in rtype else rtype)

        event_lines.append(
            f"• <code>{time_label}</code> — <b>{html.escape(err_badge)}</b> | <code>{html.escape(str(pid))}</code> ({rtype_ru})"
        )
    return "\n".join(event_lines)


def format_errors_report_helper(
    data: dict[str, Any], page: int = 0, page_size: int = 15
) -> tuple[str, InlineKeyboardMarkup]:
    """Build formatted text and pagination keyboard for network errors report."""
    total_count = data.get("total_count", 0)
    since_hours = data.get("since_hours", 24)
    if total_count == 0:
        return format_empty_errors_report(since_hours)

    by_status = data.get("by_status", {})
    status_parts = [f"{k}: {v}" for k, v in by_status.items()]
    status_line = " | ".join(status_parts) if status_parts else "нет данных"

    by_watch = data.get("by_watch", {})
    watch_lines = []
    for pid, cnt in list(by_watch.items())[:8]:
        watch_lines.append(f"• <code>{html.escape(str(pid))}</code>: {cnt}")
    if len(by_watch) > 8:
        watch_lines.append(f"<i>...и ещё {len(by_watch) - 8} поисков</i>")
    watch_block = "\n".join(watch_lines)

    events = data.get("events", [])
    total_pages = max(1, (len(events) + page_size - 1) // page_size)
    current_page = max(0, min(page, total_pages - 1))

    start_idx = current_page * page_size
    end_idx = start_idx + page_size
    page_events = events[start_idx:end_idx]

    events_block = format_error_event_lines(page_events)

    text = (
        f"⚠️ <b>Лог сетевых ошибок за последние {since_hours}ч:</b>\n\n"
        f"📊 <b>Сводка:</b>\n"
        f"• Всего ошибок: <b>{total_count}</b>\n"
        f"• По типам: {html.escape(status_line)}\n\n"
        f"🎯 <b>Ошибки по поискам:</b>\n"
        f"{watch_block}\n\n"
        f"⏱ <b>Хронология событий (MSK, стр. {current_page + 1}/{total_pages}):</b>\n"
        f"{events_block}"
    )

    nav_buttons = []
    if current_page > 0:
        nav_buttons.append(InlineKeyboardButton(text="⬅️ Предыдущие", callback_data=f"admin:errors:{current_page - 1}"))
    if current_page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton(text="Следующие ➡️", callback_data=f"admin:errors:{current_page + 1}"))

    kb_rows = []
    if nav_buttons:
        kb_rows.append(nav_buttons)
    kb_rows.append([
        InlineKeyboardButton(text="🔄 Обновить", callback_data=f"admin:errors:{current_page}"),
        InlineKeyboardButton(text=ADMIN_BTN_BACK, callback_data="admin:root"),
    ])

    return text, InlineKeyboardMarkup(inline_keyboard=kb_rows)


async def handle_admin_errors_callback(
    query: CallbackQuery,
    db_inst: Any,
    safe_edit_fn: Any,
) -> None:
    """Handle admin:errors callback showing error log with pagination."""
    page = 0
    parts = (query.data or "").split(":")
    if len(parts) >= 3 and parts[2].isdigit():
        page = int(parts[2])
    data = await db_inst.get_recent_request_errors(since_hours=24, limit=150)
    text, kb = format_errors_report_helper(data, page=page)
    if query.message:
        await safe_edit_fn(query.message, text, reply_markup=kb, parse_mode="HTML")
    await query.answer()


async def handle_errors_command(
    message: Message,
    db_inst: Any,
    safe_reply_fn: Any,
) -> None:
    """Handle /errors command showing recent request errors."""
    data = await db_inst.get_recent_request_errors(since_hours=24, limit=150)
    text, kb = format_errors_report_helper(data, page=0)
    await safe_reply_fn(message, text, reply_markup=kb, parse_mode="HTML")


async def handle_settings_command(
    message: Message,
    current_user: Any,
    safe_reply_fn: Any,
) -> None:
    """Handle /settings command showing user settings and limits."""
    qh_str = f"с {current_user.quiet_start}:00 до {current_user.quiet_end}:00" if current_user.quiet_start is not None else "выключены"
    tier_ru = {"family": "Семья", "friend": "Друг"}.get(current_user.tier, current_user.tier)
    settings = get_settings()
    multiuser_cfg = getattr(settings, "multiuser", None)
    tiers_dict = getattr(multiuser_cfg, "tiers", {}) if multiuser_cfg else {}
    tier_cfg = tiers_dict.get(current_user.tier, TierLimitConfig())
    text = (
        f"⚙️ <b>Настройки аккаунта:</b>\n\n"
        f"• Тариф: {tier_ru}\n"
        f"• Лимит товаров: {tier_cfg.max_products}\n"
        f"• Мин. интервал опроса: {tier_cfg.min_interval_minutes} мин\n"
        f"• Тихие часы: {qh_str} (/quiet для настройки)\n"
        f"• Удаление аккаунта: /delete_me"
    )
    await safe_reply_fn(message, text, parse_mode="HTML")


async def handle_my_command(
    message: Message,
    current_user: Any,
    store: Any,
    db_inst: Any,
    format_product_card_fn: Any,
    product_card_keyboard_fn: Any,
) -> None:
    """Handle /my and 'Мои товары' command listing user products."""
    from hunter.notify.telegram_handlers.public_search_menu import search_list_page

    text, keyboard = await search_list_page(current_user, db_inst)
    await message.reply(text, parse_mode="HTML", reply_markup=keyboard)
