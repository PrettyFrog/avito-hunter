"""Telegram router parts: /stats and /status commands (T-382)."""

from __future__ import annotations

import html
from typing import Any

from aiogram.types import Message
from loguru import logger

from hunter.core.models import User, now_utc
from hunter.notify.messages_ru import PRODUCT_NOT_FOUND
from hunter.notify.security import get_owned_product


def format_single_product_stats(pid: str, stats: dict[str, Any]) -> str:
    counts = stats["counts"]
    target_cnt = stats["target_model"]
    wrong_cnt = stats["wrong_model"]
    total = stats["total"]
    prec = stats["precision_pct"]

    header = f"📈 <b>Статистика обратной связи по товару <code>{html.escape(pid)}</code>:</b>\n\n"
    return (
        f"{header}"
        f"• ✅ Та модель: {target_cnt}\n"
        f"• ❌ Не та модель: {wrong_cnt}\n"
        f"• 👍 Норм: {counts.get('ok', 0)}\n"
        f"• 🚫 Фейк: {counts.get('fake', 0)}\n"
        f"• 📈 Дорого: {counts.get('expensive', 0)}\n\n"
        f"• <b>Всего оценок:</b> {total}\n"
        f"• <b>Точность (Precision):</b> {prec}%\n"
    )


async def format_overall_stats(db: Any, stats: dict[str, Any]) -> str:
    counts = stats["counts"]
    target_cnt = stats["target_model"]
    wrong_cnt = stats["wrong_model"]
    total = stats["total"]
    prec = stats["precision_pct"]

    req_by_tier = await db.get_requests_by_tier_24h()
    alt_by_tier = await db.get_alerts_by_tier_24h()
    cur_cap = await db.get_daily_cap()
    is_paused = await db.is_paused()
    auto_brake = await db.get_runtime("auto_brake_until", "")
    consec_err = await db.get_runtime("consecutive_errors", "0")

    avito_status =(
        '⏸ На '
        'паузе'
    ) if is_paused else ("⚠️ Автоторможение" if auto_brake else "🟢 В норме")
    tot_req = sum(req_by_tier.values())
    tot_alt = sum(alt_by_tier.values())

    return (
        f"📊 <b>Сводка нагрузки и статистики за 24ч:</b>\n\n"
        f"• <b>Запросы к Авито по тарифам:</b>\n"
        f"  - Владелец: {req_by_tier.get('owner', 0)}\n"
        f"  - Семья: {req_by_tier.get('family', 0)}\n"
        f"  - Друзья: {req_by_tier.get('friend', 0)}\n"
        f"  - Всего: {tot_req} / суточный предел {cur_cap}\n\n"
        f"• <b>Уведомления по тарифам:</b>\n"
        f"  - Владелец: {alt_by_tier.get('owner', 0)}\n"
        f"  - Семья: {alt_by_tier.get('family', 0)}\n"
        f"  - Друзья: {alt_by_tier.get('friend', 0)}\n"
        f"  - Всего: {tot_alt}\n\n"
        f"• <b>Состояние Авито:</b>\n"
        f"  - Статус: {html.escape(avito_status)}\n"
        f"  - Ошибок подряд: {consec_err}\n\n"
        f"• <b>Обратная связь (всего оценок: {total}, точность: {prec}%):</b>\n"
        f"  - ✅ Та модель: {target_cnt} | ❌ Не та: {wrong_cnt}\n"
        f"  - 👍 Норм: {counts.get(
            'o'
            'k', 0)} | 🚫 Фейк: {counts.get('fake', 0)} | 📈 Дорого: {counts.get('expensive', 0)}\n"
    )


async def handle_stats_command(
    message: Message,
    current_user: User,
    db: Any,
    store: Any,
    safe_reply_fn: Any,
) -> None:
    parts = (message.text or "").split(maxsplit=1)
    pid = parts[1].strip() if len(parts) > 1 else None

    if pid:
        p = get_owned_product(current_user, pid, store)
        if not p:
            await safe_reply_fn(message, PRODUCT_NOT_FOUND)
            return

    stats = await db.get_feedback_stats(product_id=pid)
    if pid:
        text = format_single_product_stats(pid, stats)
        await safe_reply_fn(message, text, parse_mode="HTML")
        return

    text = await format_overall_stats(db, stats)
    await safe_reply_fn(message, text, parse_mode="HTML")


async def handle_status_command(
    message: Message,
    current_user: User,
    db: Any,
    store: Any,
    safe_reply_fn: Any,
) -> None:
    from hunter.notify.telegram_handlers.public_status import get_public_status_text

    text = await get_public_status_text(current_user, db)
    await safe_reply_fn(message, text, parse_mode="HTML")


async def handle_why_command(message: Message, db: Any, safe_reply_fn: Any) -> None:
    await safe_reply_fn(
        message,
        "ℹ️ Команда /why устарела в LotRadar v2. Вся информация о найденных лотах доступна в уведомлениях и по команде /found.",
    )


async def handle_ref_command(
    message: Message,
    current_user: User,
    store: Any,
    db: Any,
) -> None:
    await message.reply(
        "ℹ️ Добавление фото-эталонов (/ref) устарело в LotRadar v2. Поиск работает напрямую по ссылкам Авито (/add).",
    )
