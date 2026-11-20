"""Telegram router parts: handle_apply_stage callback and /limits command (T-380)."""

from __future__ import annotations

import html
from typing import Any

from aiogram.types import CallbackQuery, Message
from loguru import logger

from hunter.core.product import ProductStore, save_product_profile
from hunter.notify.messages_ru import (
    LIMITS_UPDATED_TEMPLATE,
    LIMITS_USAGE,
    LIMITS_USER_NOT_FOUND,
)


async def handle_apply_stage_callback(
    callback: CallbackQuery,
    settings: Any,
    store: Any,
    safe_reply_fn: Any,
) -> None:
    await callback.answer("Ступени опроса устарели в LotRadar v2.", show_alert=True)


def parse_limits_args(parts: list[str]) -> tuple[int | None, int | None]:
    max_products = None
    min_interval = None
    for p in parts[2:]:
        p_lower = p.lower()
        if p_lower.startswith(("товаров=", "товары=", "products=")):
            try:
                val = int(p.split("=")[1])
                if val >= 0:
                    max_products = val
            except ValueError:
                pass
        elif p_lower.startswith(("интервал=", "interval=")):
            try:
                val = int(p.split("=")[1])
                if val >= 1:
                    min_interval = val
            except ValueError:
                pass
    return max_products, min_interval


async def handle_limits_command(message: Message, database: Any) -> None:
    text = (message.text or "").strip()
    parts = text.split()
    if len(parts) < 2:
        await message.reply(LIMITS_USAGE)
        return

    target_ident = parts[1]
    user_row = await database.find_user_by_identifier(target_ident)
    if not user_row:
        await message.reply(LIMITS_USER_NOT_FOUND)
        return

    max_products, min_interval = parse_limits_args(parts)
    if max_products is None and min_interval is None:
        await message.reply(LIMITS_USAGE)
        return

    await database.update_user_limits(user_row["id"], max_products=max_products, min_interval_minutes=min_interval)
    name = user_row["username"] or user_row["display_name"] or str(user_row["telegram_user_id"])
    if user_row["username"]:
        name = f"@{name.lstrip('@')}"
    res_max = max_products if max_products is not None else user_row.get("max_products", 3)
    res_int = min_interval if min_interval is not None else user_row.get("min_interval_minutes", 15)
    await message.reply(LIMITS_UPDATED_TEMPLATE.format(name=name, max_products=res_max, min_interval=res_int))


async def format_user_line(u: dict[str, Any], store: Any, database: Any) -> str:
    u_id = u["id"]
    u_name = u["username"]
    disp_name = u["display_name"]
    role = u["role"]
    tier = u["tier"]
    status = u["status"]
    max_prods = u["max_products"]
    min_int = u["min_interval_minutes"]

    name_repr = f"@{u_name}" if u_name else (disp_name or f"id={u['telegram_user_id']}")
    role_repr = "Владелец" if role == "owner" else ("Семья" if tier == "family" else "Друг")
    status_icon = "🟢" if status == "active" else ("⏸" if status == "blocked" else "⚪")

    user_prods = store.get_for_user(u_id)
    prods_count = len(user_prods)

    findings_7d = await database.get_user_findings_7d(u_id)
    pos_fb, neg_fb = await database.get_user_feedback_counts(u_id)

    return (
        f"• {status_icon} <b>{html.escape(name_repr)}</b> ({html.escape(role_repr)}) — {html.escape(str(status))}\n"
        f"  Товаров: {prods_count}/{max_prods} | Мин. интервал: {min_int}м\n"
        f"  Находок за 7д: {findings_7d} | Реакции: 👍 {pos_fb} / 👎 {neg_fb}\n"
    )


async def handle_users_command(
    message: Message, database: Any, store: Any, safe_reply_fn: Any
) -> None:
    all_users = await database.get_all_users()
    if not all_users:
        await message.reply("Список пользователей пуст.")
        return

    lines = [f"👥 <b>Пользователи бота ({len(all_users)}):</b>\n"]
    for u in all_users:
        line = await format_user_line(u, store, database)
        lines.append(line)

    await safe_reply_fn(message, "\n".join(lines), parse_mode="HTML")


async def handle_budget_command(
    message: Message, db: Any, store: Any, safe_reply_fn: Any
) -> None:
    await safe_reply_fn(message, "Команда /budget устарела. В LotRadar v2 мониторинг работает на слотах через /status.")
