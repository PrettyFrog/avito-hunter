"""Telegram router parts: cmd_invite and cmd_invites helpers (T-359)."""

from __future__ import annotations

from datetime import timedelta
import hashlib
import secrets
from typing import Any

from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from hunter.core.models import now_utc
from hunter.notify.messages_ru import (
    FAQ_ITEMS,
    FAQ_MENU_TITLE,
    HELP_OWNER_EXTRA,
    HELP_USER_TEXT,
    INVITE_CREATED_TEMPLATE,
    INVITE_FORWARD_TEMPLATE,
    INVITE_LIMIT_REACHED,
    INVITE_REVOKED_SUCCESS_TEMPLATE,
    INVITE_USAGE,
    INVITES_LIST_EMPTY,
    INVITES_LIST_HEADER,
    clean_name,
)


async def handle_create_invite_command(
    message: Message,
    db_inst: Any,
    current_user: Any,
) -> None:
    """Handle /invite <label> [tier] command for owner."""
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 2:
        await message.reply(INVITE_USAGE, parse_mode="HTML")
        return

    label = parts[1].strip()
    raw_tier = parts[2].strip().lower() if len(parts) >= 3 else "friend"
    if raw_tier not in ("семья", "family", "друг", "friend"):
        await message.reply(INVITE_USAGE, parse_mode="HTML")
        return
    tier = "family" if raw_tier in ("семья", "family") else "friend"

    active_count = await db_inst.get_active_invites_count(current_user.id)
    if active_count >= 10:
        await message.reply(INVITE_LIMIT_REACHED, parse_mode="HTML")
        return

    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    expires_at = (now_utc() + timedelta(hours=48)).isoformat()
    await db_inst.create_invite(
        token_hash=token_hash,
        label=label,
        tier=tier,
        created_by=current_user.id,
        expires_at=expires_at,
    )

    bot_username = "avito_hunter_bot"
    if message.bot:
        try:
            me = await message.bot.get_me()
            if me.username:
                bot_username = me.username
        except Exception:
            pass

    link = f"https://t.me/{bot_username}?start=inv_{raw_token}"
    fwd_text = INVITE_FORWARD_TEMPLATE.format(link=link)
    tier_ru = "Семья" if tier == "family" else "Друг"
    await message.reply(
        INVITE_CREATED_TEMPLATE.format(
            name=clean_name(label),
            tier_ru=tier_ru,
            link=link,
            forward_text=fwd_text,
        ),
        parse_mode="HTML",
    )


async def handle_list_invites_command(
    message: Message,
    db_inst: Any,
    current_user: Any,
) -> None:
    """Handle /invites command listing active invites with revoke buttons."""
    invites = await db_inst.get_active_invites(current_user.id)
    if not invites:
        await message.reply(INVITES_LIST_EMPTY, parse_mode="HTML")
        return

    lines = [INVITES_LIST_HEADER]
    kb_rows = []
    for inv in invites:
        tier_ru = "Семья" if inv["tier"] == "family" else "Друг"
        label = clean_name(inv.get("label") or "Без имени")
        exp_iso = inv.get("expires_at", "")[:16].replace("T", " ")
        lines.append(f"• <b>{label}</b> ({tier_ru}) — до {exp_iso} UTC")
        kb_rows.append([InlineKeyboardButton(text=f"Отозвать {label}", callback_data=f"revoke_inv:{inv['id']}")])

    await message.reply(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
        parse_mode="HTML",
    )


async def handle_revoke_invite_callback(
    callback: CallbackQuery,
    db_inst: Any,
) -> None:
    """Handle revoke_inv:<id> callback query from owner."""
    parts = (callback.data or "").split(":")
    if len(parts) < 2:
        return
    inv_id = int(parts[1])
    inv = await db_inst.get_invite_by_id(inv_id)
    ok = await db_inst.revoke_invite(inv_id)
    label = clean_name(inv.get("label") if inv else "")
    if ok:
        await callback.answer(INVITE_REVOKED_SUCCESS_TEMPLATE.format(name=label), show_alert=True)
        if callback.message:
            await callback.message.edit_text(INVITE_REVOKED_SUCCESS_TEMPLATE.format(name=label), parse_mode="HTML")
    else:
        await callback.answer("Приглашение уже отозвано или не найдено.", show_alert=True)


_OWNER_ONLY_COMMANDS = {
    "admin", "broadcast", "tickets", "users", "stats", "limits", "cap",
    "budget", "status", "why", "explain", "show", "ref", "remove", "edit", "invite", "invites",
    "errors", "errs",
}

_CMD_DETAILS = {
    "start": "<b>/start</b> — перезапуск бота, проверка статуса и главное меню.",
    "help": "<b>/help</b> — полный список доступных команд для вашего профиля.",
    "add":         '<b>/add</b> — добавление товара для '
        'поиска.\nФормат: /add Название | цена | ссылка на '
        'поиск Авито.',
    "my": "<b>/my</b> — просмотр ваших отслеживаемых товаров, изменение цены и управление паузой.",
    "found": "<b>/found</b> — список недавно найденных подходящих объявлений.",
    "pause": "<b>/pause</b> — временная приостановка поиска по товарам.",
    "pause_all": "<b>/pause_all</b> — временная приостановка поиска по всем товарам.",
    "resume": "<b>/resume</b> — возобновление поиска по товарам.",
    "resume_all": "<b>/resume_all</b> — возобновление поиска по всем товарам.",
    "quiet":         '<b>/quiet</b> — настройка тихих часов, когда бот '
        'не присылает уведомления (например, /quiet 23-8 '
        'или /quiet off).',
    "settings": "<b>/settings</b> — персональные настройки поиска и оповещений.",
    "guide": "<b>/guide</b> — подробное пошаговое руководство по использованию бота.",
    "faq": "<b>/faq</b> — ответы на часто задаваемые вопросы.",
    "support": "<b>/support</b> — прямая связь с владельцем бота.",
    "delete_me": "<b>/delete_me</b> — полное удаление всех ваших данных и товаров из бота.",
    "admin": "<b>/admin</b> — интерактивная панель управления (только для владельца).",
    "broadcast":         '<b>/broadcast</b> — массовая рассылка сообщений '
        'пользователям (только для владельца).',
    "tickets": "<b>/tickets</b> — просмотр открытых обращений в поддержку (только для владельца).",
    "invite":         '<b>/invite [Имя] [семья|друг]</b> — создание '
        'одноразового приглашения для нового пользователя.',
    "invites": "<b>/invites</b> — список всех активных приглашений и возможность их отозвать.",
    "users": "<b>/users</b> — список пользователей бота и их товаров (только для владельца).",
    "stats": "<b>/stats</b> — подробная статистика работы сервиса (только для владельца).",
    "errors":         '<b>/errors</b> — лог и сводка сетевых ошибок (502,'
        ' таймауты, 429/403) за 24 часа (только для '
        'владельца).',
    "errs": "<b>/errs</b> — синоним /errors (только для владельца).",
    "limits":         '<b>/limits &lt;ID&gt; max_products=N</b> — '
        'просмотр и изменение лимитов пользователя (только '
        'для владельца).',
    "cap": "<b>/cap &lt;N&gt;</b> — установка суточного лимита запросов (только для владельца).",
    "budget": "<b>/budget</b> — текущее состояние бюджета запросов (только для владельца).",
    "status": "<b>/status</b> — проверка работы сервиса (только для владельца).",
    "why":         '<b>/why &lt;ID или ссылка&gt;</b> — подробный '
        'аудит лота: фильтры, вердикт, статус валидации '
        'фото (только для владельца).',
    "explain":         '<b>/explain &lt;ID или ссылка&gt;</b> — подробный '
        'аудит лота (синоним /why, только для владельца).',
    "show": "<b>/show &lt;ID&gt;</b> — просмотр подробной информации о товаре (только для владельца).",
    "ref": "<b>/ref &lt;ID&gt;</b> — загрузка эталонной фотографии для товара (только для владельца).",
    "remove": "<b>/remove &lt;ID&gt;</b> — удаление товара (только для владельца).",
    "edit": "<b>/edit &lt;ID&gt;</b> — редактирование параметров товара (только для владельца).",
}


async def handle_help_command(
    message: Message,
    current_user: Any,
) -> None:
    """Handle /help command with per-command details and owner extra text."""
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) > 1:
        cmd = parts[1].strip().lower().lstrip("/")
        if cmd in _OWNER_ONLY_COMMANDS and current_user.role != "owner":
            await message.reply("Команда доступна только владельцу бота.", parse_mode="HTML")
            return
        if cmd in _CMD_DETAILS:
            await message.reply(_CMD_DETAILS[cmd], parse_mode="HTML")
            return

    if current_user.role == "owner":
        text = HELP_USER_TEXT + HELP_OWNER_EXTRA
    else:
        text = HELP_USER_TEXT
    await message.reply(text, parse_mode="HTML")


async def handle_faq_command(message: Message) -> None:
    """Handle /faq or '❓ Помощь' message."""
    kb_rows = []
    for key, (q_text, _) in FAQ_ITEMS.items():
        kb_rows.append([InlineKeyboardButton(text=q_text, callback_data=f"faq:{key}")])
    await message.reply(
        FAQ_MENU_TITLE,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
        parse_mode="HTML",
    )
