"""Tenant-scoped Telegram search selection and detail views."""

from __future__ import annotations

import hashlib
import html
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from hunter.core.models import User
from hunter.notify.telegram_handlers.public_status import _get_repo
from hunter.storage.subscriptions import (
    AccessDeniedError,
    SubscriptionActor,
    SubscriptionNotFoundError,
)


def search_token(sub_id: str) -> str:
    return hashlib.sha256(sub_id.encode("utf-8")).hexdigest()[:24]


async def search_list_page(user: User, db: Any, page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    subs = await _get_repo(db).list_for_actor(SubscriptionActor(user.id))
    page_size = 10
    pages = max(1, (len(subs) + page_size - 1) // page_size)
    page = max(0, min(page, pages - 1))
    current = subs[page * page_size : (page + 1) * page_size]
    rows = []
    lines = [f"📋 <b>Ваши поиски ({len(subs)})</b>"]
    for sub in current:
        name = sub.name or "Без названия"
        state = "🟢" if sub.enabled else "⏸"
        lines.append(f"{state} {html.escape(name)} — до {sub.max_price:,} ₽".replace(",", " "))
        callback = f"sm:view:{search_token(sub.id)}"
        rows.append([InlineKeyboardButton(text=name, callback_data=callback)])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"sm:page:{page - 1}"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"sm:page:{page + 1}"))
    if nav:
        rows.append(nav)
    if not subs:
        lines.append("Пока нет поисков. Добавьте первый через /add.")
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def _display_value(value: str) -> str:
    return html.escape(value if len(value) <= 700 else value[:700] + "…")


async def search_card(user: User, db: Any, sub_id: str) -> tuple[str, InlineKeyboardMarkup] | None:
    repo = _get_repo(db)
    try:
        sub = await repo.get(SubscriptionActor(user.id), sub_id)
    except (AccessDeniedError, SubscriptionNotFoundError):
        return None
    reader = await repo.mgr.open_read_connection()
    try:
        cur = await reader.execute(
            "SELECT canonical_url FROM queries WHERE id = ?", (sub.query_id,)
        )
        row = await cur.fetchone()
    finally:
        await repo.mgr.close_reader(reader)
    name = html.escape(sub.name or "Без названия")
    url = _display_value(str(row[0]) if row else "недоступна")
    words = _display_value(sub.manual_stopwords or "нет")
    keywords = _display_value(getattr(sub, "manual_keywords", "") or "нет")
    state = "активен" if sub.enabled else "на паузе"
    price = f"{sub.max_price:,}".replace(",", " ")
    text = (
        f"🔎 <b>{name}</b>\nСсылка: <code>{url}</code>\n"
        f"Потолок: <b>{price} ₽</b>\nСтоп-слова: {words}\n"
        f"Ключевые слова: {keywords}\nСтатус: {state}"
    )
    return text, _search_keyboard(sub)


def _search_keyboard(sub: Any) -> InlineKeyboardMarkup:
    token = search_token(sub.id)
    btn_stop = InlineKeyboardButton(
        text="🚫 Стоп-слова", callback_data=f"sm:edit:{token}:{sub.version}:words"
    )
    btn_kw = InlineKeyboardButton(
        text="🔑 Ключевые слова", callback_data=f"sm:edit:{token}:{sub.version}:keywords"
    )
    rows = [
        [InlineKeyboardButton(text="✏️ Название", callback_data=f"sm:edit:{token}:{sub.version}:name")],
        [btn_stop, btn_kw],
        [InlineKeyboardButton(text="💰 Цена", callback_data=f"sm:edit:{token}:{sub.version}:price")],
        [InlineKeyboardButton(text="🔗 Ссылка", callback_data=f"sm:edit:{token}:{sub.version}:url")],
    ]
    action = "pause" if sub.enabled else "resume"
    rows.append([
        InlineKeyboardButton(text="⏸ Пауза" if sub.enabled else "▶️ Возобновить",
                             callback_data=f"sc:{action}:{token}:{sub.version}"),
        InlineKeyboardButton(text="🗑 Удалить", callback_data=f"sc:delete:{token}:{sub.version}"),
    ])
    rows.append([InlineKeyboardButton(text="◀️ К списку", callback_data="sm:page:0")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
