"""Telegram router parts: /edit command handling (T-378)."""

from __future__ import annotations

from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from loguru import logger

from hunter.core.models import User
from hunter.core.product import save_product_profile
from hunter.notify.messages_ru import PRODUCT_NOT_FOUND
from hunter.notify.security import get_owned_product
from hunter.notify.telegram_router_parts.setup_telegram_router_registration_136 import (
    handle_edit_max_price,
    handle_edit_subscription_if_needed,
)


def apply_schedule_or_policy_field(p: Any, field_name: str, field_val: str) -> str | None:
    if field_name == "poll_seconds":
        if field_val.strip().lower() in ("auto", "none", "null", "default"):
            p.poll_seconds = None
        else:
            val = int(field_val)
            if val < 1:
                return "❌ Интервал опроса poll_seconds должен быть не менее 1 секунды."
            p.poll_seconds = val
            p.poll_minutes = max(1, val // 60)
    elif field_name == "poll_minutes":
        if field_val.strip().lower() in ("auto", "none", "null", "default"):
            p.poll_minutes = None
            p.poll_seconds = None
        else:
            val = int(field_val)
            if val < 1:
                return "❌ Интервал опроса poll_minutes должен быть не менее 1 минуты."
            p.poll_minutes = val
            p.poll_seconds = val * 60
    elif field_name == "priority":
        if field_val not in ("high", "normal", "low"):
            raise ValueError("priority must be high, normal, or low")
        p.priority = field_val
    elif field_name == "mode":
        if field_val not in ("live", "shadow", "off"):
            raise ValueError("mode must be live, shadow, or off")
        p.mode = field_val
    elif field_name == "policy":
        if field_val not in ("strict", "loose"):
            raise ValueError("policy must be strict or loose")
        p.match.policy = field_val
    return None


async def handle_edit_schedule_or_policy(
    message: Any,
    p: Any,
    pid: str,
    field_name: str,
    field_val: str,
    current_user: User,
    store: Any,
    db: Any,
    settings: Any,
    calculate_forecast_fn: Any,
) -> None:
    err = apply_schedule_or_policy_field(p, field_name, field_val)
    if err:
        await message.reply(err)
        return

    # Recalculate whole-system forecast budget against 80% planned cap
    all_profiles, _ = store.load_all()
    all_profiles[pid] = p
    forecast = calculate_forecast_fn(list(all_profiles.values()))
    daily_cap = await db.get_daily_cap()
    planned_limit = int(daily_cap * 0.80)
    if forecast > planned_limit:
        if current_user.role != "owner":
            await message.reply(
                f"❌ Изменение отклонено: прогнозируемый расход превысит лимит {planned_limit} запросов/сут "
                f"(80% от суточного лимита {daily_cap}): {forecast:.1f} > {planned_limit}."
            )
            return
        logger.warning(
            "Owner budget override: forecast {:.1f} > planned {} (cap {})",
            forecast,
            planned_limit,
            daily_cap,
        )

    save_product_profile(p, directory=settings.data_dir / "products")
    await message.reply(
        f"✅ Поле `{field_name}` товара `{pid}` успешно обновлено на `{field_val}`. "
        f"Прогноз расхода: {forecast:.1f} / {planned_limit} запросов/сут (лимит {daily_cap})."
    )


async def handle_edit_photos(
    message: Any, p: Any, pid: str, field_name: str, field_val: str, settings: Any
) -> None:
    if field_name == "photo_check":
        if field_val not in ("off", "warn", "strict"):
            raise ValueError("photo_check must be off, warn, or strict")
        p.photos.photo_check = field_val
        save_product_profile(p, directory=settings.data_dir / "products")
        await message.reply(f"✅ Режим проверки фото `photo_check` товара `{pid}` успешно обновлен на `{field_val}`.")
    elif field_name == "photos.check":
        if field_val not in ("off", "shadow", "describe", "compare", "both"):
            raise ValueError("photos.check must be off, shadow, describe, compare, or both")
        p.photos.check = field_val
        save_product_profile(p, directory=settings.data_dir / "products")
        await message.reply(f"✅ Поле `{field_name}` товара `{pid}` успешно обновлено на `{field_val}`.")


async def handle_edit_command(
    message: Message,
    current_user: User,
    store: Any,
    db: Any,
    settings: Any,
    calculate_forecast_fn: Any,
) -> None:
    if await handle_edit_subscription_if_needed(message, current_user, db):
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3 or "=" not in parts[2]:
        await message.reply(
            "Использование: `/edit <product_id> поле=значение`\n"
            "Пример: `/edit thinkpad_t14 max_price=48000`\n"
            'Допустимые поля: `max_price`, `priority`, `mode`, `policy`, '
                '`poll_seconds`, `poll_minutes`, `photos.check`, `photo_check`'
        )
        return
    pid = parts[1].strip()
    field_expr = parts[2].strip()
    field_name, field_val = field_expr.split("=", 1)
    field_name = field_name.strip()
    field_val = field_val.strip()

    p = get_owned_product(current_user, pid, store)
    if not p:
        await message.reply(PRODUCT_NOT_FOUND)
        return

    try:
        if field_name == "max_price":
            await handle_edit_max_price(message, p, pid, field_val, settings)
        elif field_name in ("priority", "poll_minutes", "poll_seconds", "mode", "policy"):
            await handle_edit_schedule_or_policy(
                message, p, pid, field_name, field_val, current_user, store, db, settings, calculate_forecast_fn
            )
        elif field_name in ("photo_check", "photos.check"):
            await handle_edit_photos(message, p, pid, field_name, field_val, settings)
        else:
            await message.reply(f"❌ Неподдерживаемое поле `{field_name}`.")
    except Exception as exc:
        await message.reply(f"❌ Ошибка валидации значения: {exc}")


async def handle_cap_command(
    message: Message, db: Any, store: Any, safe_reply_fn: Any
) -> None:
    await safe_reply_fn(
        message,
        "Команда /cap устарела. В LotRadar v2 лимиты и опрос управляются через слоты и политику мониторинга.",
    )
