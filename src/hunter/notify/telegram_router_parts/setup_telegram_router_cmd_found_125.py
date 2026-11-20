"""Telegram router parts: /found, /list, and /show commands (T-373)."""

from __future__ import annotations

import html
import json
import logging
import math
import os
from pathlib import Path
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from hunter.core.config import get_settings
from hunter.core.models import now_utc
from hunter.core.product import clear_product_pause, save_product_profile
from hunter.notify.broadcast import set_global_pause_with_broadcast
from hunter.notify.messages_ru import (
    FOUND_EMPTY,
    FOUND_HEADER,
    PAUSE_ALL_CONFIRM_TEMPLATE,
    PAUSE_ALL_NONE,
    PAUSE_ALL_OWNER_DONE,
    PAUSE_SUCCESS_TEMPLATE,
    PRODUCT_NOT_FOUND,
    RESUME_ALL_CONFIRM_TEMPLATE,
    RESUME_ALL_NONE,
    RESUME_ALL_OWNER_DONE,
    RESUME_SUCCESS_TEMPLATE,
)
from hunter.notify.security import get_owned_product
from hunter.notify.telegram_alert_card import format_relative_time

logger = logging.getLogger(__name__)


async def _get_user_recent_v2_findings(
    db_inst: Any, user_id: int, limit: int = 10
) -> list[dict[str, Any]]:
    rows: list[Any] = []
    mgr = getattr(db_inst, "storage_mgr", None) or getattr(db_inst, "mgr", None)
    if mgr is not None and hasattr(mgr, "open_read_connection"):
        reader = await mgr.open_read_connection()
        try:
            cur = await reader.execute(
                "SELECT payload, created_at FROM deliveries WHERE user_id = ? ORDER BY rowid DESC LIMIT ?",
                (user_id, limit),
            )
            rows = await cur.fetchall()
        finally:
            await mgr.close_reader(reader)
    elif hasattr(db_inst, "connect"):
        conn = await db_inst.connect()
        cur = await conn.execute(
            "SELECT payload, created_at FROM deliveries WHERE user_id = ? ORDER BY rowid DESC LIMIT ?",
            (user_id, limit),
        )
        rows = await cur.fetchall()
    else:
        return []

    res: list[dict[str, Any]] = []
    for r in rows:
        payload_raw, created_at = r[0], r[1]
        p = json.loads(payload_raw) if isinstance(payload_raw, str) else (payload_raw or {})
        title = p.get("title") or "Товар"
        price = p.get("new_price") or p.get("price")
        url = p.get("url") or "https://www.avito.ru"
        time_ago = p.get("time_ago") or format_relative_time(p.get("published_at") or created_at)
        res.append({"title": title, "price": price, "url": url, "time_ago": time_ago})
    return res


async def _count_user_subs_by_state(db_inst: Any, user_id: int, is_owner: bool, enabled: bool) -> int:
    if db_inst is None:
        return 0
    try:
        from hunter.notify.telegram_handlers.public_status import _get_repo
        from hunter.storage.subscriptions import SubscriptionActor

        repo = _get_repo(db_inst)
        subs = await repo.list_for_actor(SubscriptionActor(user_id=user_id, is_owner=is_owner))
        return sum(1 for s in subs if s.enabled == enabled)
    except Exception as exc:
        logger.warning("failed to count subscriptions: %s", exc)
        return 0


async def _resolve_active_count(db_inst: Any, current_user: Any, store: Any) -> int:
    subs = await _count_user_subs_by_state(db_inst, current_user.id, current_user.role == "owner", enabled=True)
    if subs > 0:
        return subs
    if store is not None and hasattr(store, "get_for_user"):
        return sum(1 for p in store.get_for_user(current_user.id).values() if p.mode != "off")
    return 0


async def _resolve_paused_count(db_inst: Any, current_user: Any, store: Any) -> int:
    subs = await _count_user_subs_by_state(db_inst, current_user.id, current_user.role == "owner", enabled=False)
    if subs > 0:
        return subs
    if store is not None and hasattr(store, "get_for_user"):
        return sum(1 for p in store.get_for_user(current_user.id).values() if p.mode == "off")
    return 0


async def handle_found_command(
    message: Message,
    current_user: Any,
    store: Any,
    db_inst: Any,
    get_user_recent_findings_fn: Any,
) -> None:
    """Handle /found command displaying recent findings for user products or subscriptions."""
    findings: list[dict[str, Any]] = []
    if db_inst is not None:
        try:
            findings = await _get_user_recent_v2_findings(db_inst, current_user.id, limit=10)
        except Exception as exc:
            logger.warning("failed to query v2 deliveries: %s", exc)

    if not findings and store is not None and hasattr(store, "get_for_user"):
        user_prods = store.get_for_user(current_user.id)
        if user_prods:
            try:
                findings = await get_user_recent_findings_fn(
                    db_inst, current_user.id, list(user_prods.keys()), limit=10
                )
            except Exception:
                pass

    if not findings:
        await message.reply(FOUND_EMPTY, parse_mode="HTML")
        return

    lines = [FOUND_HEADER]
    for f in findings:
        pr_val = f"{f['price']:,} ₽".replace(",", " ") if f.get("price") else "цена не указана"
        url = f.get("url") or "https://www.avito.ru"
        lines.append(f"• <a href=\"{url}\">{html.escape(f['title'])}</a> — {pr_val} ({f['time_ago']})")
    await message.reply("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)


async def handle_list_command(
    message: Message,
    current_user: Any,
    store: Any,
    db_inst: Any,
    safe_reply_fn: Any,
) -> None:
    """Handle /list command displaying active tracked products."""
    from hunter.notify.telegram_handlers.public_search_menu import search_list_page
    text, keyboard = await search_list_page(current_user, db_inst)
    await safe_reply_fn(message, text, parse_mode="HTML", reply_markup=keyboard)


async def handle_show_command(
    message: Message,
    current_user: Any,
    store: Any,
    safe_reply_fn: Any,
) -> None:
    """Handle /show command showing product profile details."""
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await safe_reply_fn(message,
            'Использование: <code>/show '
            '&lt;product_id&gt;</code>', parse_mode="HTML")
        return
    pid = parts[1].strip()
    p = get_owned_product(current_user, pid, store)
    if not p:
        await safe_reply_fn(message, PRODUCT_NOT_FOUND)
        return

    aliases_str = ", ".join(p.match.aliases[:5]) + ("..." if len(p.match.aliases) > 5 else "")
    reject_str = ", ".join(p.match.reject[:5]) + ("..." if len(p.match.reject) > 5 else "") or "нет"
    refs_count = len(p.photos.refs)
    refs_status = "✅ проверены" if p.photos.refs_verified else "⚠️ не проверены (эксперимент)"

    min_price_str = f"{p.min_price:,} ₽" if getattr(p,
        'min_'
        'price', None) else "нет (все лоты от 0 ₽)"
    search_ceiling = int(math.ceil(p.max_price * (1.0 + p.headroom_pct)))
    text = (
        f"📦 <b>Товар:</b> <code>{html.escape(p.id)}</code> ({html.escape(p.name)})\n\n"
        f"• <b>Действующий потолок:</b> {p.max_price:,} ₽ (в поиске: {search_ceiling:,} ₽, headroom +{int(p.headroom_pct*100)}%)\n"
        f"• <b>Нижний порог:</b> {html.escape(min_price_str)}\n"
        f"• <b>Режим:</b> <code>{html.escape(str(p.mode))}</code> | Приоритет: <code>{html.escape(str(p.priority))}</code>\n"
        f"• <b>Политика сопоставления:</b> <code>{html.escape(str(p.match.policy))}</code> (пресет: <code>{html.escape(str(p.preset))}</code>)\n"
        f"• <b>Алиасы:</b> {html.escape(aliases_str or 'нет')}\n"
        f"• <b>Исключения (reject):</b> {html.escape(reject_str)}\n"
        f"• <b>Фото-проверка:</b> <code>{html.escape(str(p.photos.check))}</code> (эталонов: {refs_count}, статус: {refs_status})\n"
        f"• <b>Покрытие:</b> backfill=<code>{p.coverage.backfill}</code>, sweep=<code>{html.escape(str(p.coverage.deep_sweep))}</code>, max_pages={p.coverage.max_pages}\n"
        f'• <b>Ссылка поиска:</b> <a href="{html.escape(p.search_url)}">Открыть на Авито</a>'
    )
    await safe_reply_fn(message, text, parse_mode="HTML", disable_web_page_preview=True)


async def is_system_globally_paused(db_inst: Any, settings: Any) -> bool:
    """Check if hunter is currently under global pause or captcha block."""
    is_paused = await db_inst.is_paused()
    captcha_flag = await db_inst.get_runtime("captcha", "0")
    data_dir = Path(os.environ.get("HUNTER_DATA_DIR") or settings.data_dir)
    flag_pause = data_dir / "pause.flag"
    flag_captcha = data_dir / "captcha.flag"
    return bool(is_paused or captcha_flag == "1" or flag_pause.exists() or flag_captcha.exists())


async def handle_pause_all_command(
    message: Message,
    db_inst: Any,
    channel_bot: Any = None,
) -> None:
    """Handle /pause_all command by owner to set global pause."""
    bot = message.bot or channel_bot
    await set_global_pause_with_broadcast(db_inst, paused=True, bot=bot)
    await message.reply(PAUSE_ALL_OWNER_DONE)


async def handle_pause_command(
    message: Message,
    current_user: Any,
    store: Any,
    db_inst: Any,
    channel_bot: Any = None,
) -> None:
    """Handle /pause command for single product, subscription, global or bulk."""
    parts = (message.text or "").split(maxsplit=1)
    settings = get_settings()

    if len(parts) >= 2:
        pid = parts[1].strip()
        if pid.startswith("sub_"):
            from hunter.notify.telegram_handlers.public_status import pause_subscription
            _, msg = await pause_subscription(current_user, pid, db_inst)
            await message.reply(msg, parse_mode="HTML")
            return
        if current_user.role == "owner" and pid.lower() in ("all", "bot", "global", "system"):
            bot = message.bot or channel_bot
            await set_global_pause_with_broadcast(db_inst, paused=True, bot=bot)
            await message.reply(PAUSE_ALL_OWNER_DONE)
            return
        p = get_owned_product(current_user, pid, store) if store else None
        if not p:
            if db_inst is not None:
                from hunter.notify.telegram_handlers.public_status import pause_subscription
                ok, msg = await pause_subscription(current_user, f"sub_{pid}", db_inst)
                if ok:
                    await message.reply(msg, parse_mode="HTML")
                    return
            await message.reply(PRODUCT_NOT_FOUND)
            return
        p.mode = "off"
        p.paused_reason = "user"
        save_product_profile(p, directory=getattr(store, "directory", settings.data_dir / "products"))
        await message.reply(PAUSE_SUCCESS_TEMPLATE.format(name=p.name))
        return

    # Bulk pause confirmation (active subscriptions in db, fallback to store)
    active_count = await _resolve_active_count(db_inst, current_user, store)
    if active_count == 0:
        await message.reply(PAUSE_ALL_NONE)
        return

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Да, поставить на паузу", callback_data="pause_all_confirm")],
            [InlineKeyboardButton(text="Отмена", callback_data="pause_all_cancel")],
        ]
    )
    await message.reply(PAUSE_ALL_CONFIRM_TEMPLATE.format(count=active_count), reply_markup=kb)


async def _resume_single_target(
    message: Message,
    pid: str,
    current_user: Any,
    store: Any,
    db_inst: Any,
    settings: Any,
) -> None:
    if pid.startswith("sub_"):
        from hunter.notify.telegram_handlers.public_status import resume_subscription

        _, msg = await resume_subscription(current_user, pid, db_inst)
        await message.reply(msg, parse_mode="HTML")
        return
    p = get_owned_product(current_user, pid, store) if store else None
    if not p:
        if db_inst is not None:
            from hunter.notify.telegram_handlers.public_status import resume_subscription

            ok, msg = await resume_subscription(current_user, f"sub_{pid}", db_inst)
            if ok:
                await message.reply(msg, parse_mode="HTML")
                return
        await message.reply(PRODUCT_NOT_FOUND)
        return
    ok, err_msg = clear_product_pause(p, actor_role=current_user.role)
    if not ok:
        await message.reply(err_msg)
        return
    p.last_confirmed_at = now_utc().isoformat()
    save_product_profile(p, directory=getattr(store, "directory", settings.data_dir / "products"))
    await message.reply(RESUME_SUCCESS_TEMPLATE.format(name=p.name))


async def handle_resume_command(
    message: Message,
    current_user: Any,
    store: Any,
    db_inst: Any,
    channel_bot: Any = None,
) -> None:
    """Handle /resume command for single product, subscription, global or bulk."""
    parts = (message.text or "").split(maxsplit=1)
    settings = get_settings()

    if current_user.role == "owner":
        is_globally_paused = await is_system_globally_paused(db_inst, settings)
        if (len(parts) >= 2 and parts[1].strip().lower() in ("all", "bot", "global", "system")) or (len(parts) < 2 and is_globally_paused):
            bot = message.bot or channel_bot
            await set_global_pause_with_broadcast(db_inst, paused=False, bot=bot)
            await message.reply(RESUME_ALL_OWNER_DONE)
            return

    if len(parts) >= 2 and parts[1].strip().lower() not in ("all", "bot", "global", "system"):
        await _resume_single_target(message, parts[1].strip(), current_user, store, db_inst, settings)
        return

    # Bulk resume confirmation
    paused_count = await _resolve_paused_count(db_inst, current_user, store)
    if paused_count == 0:
        if current_user.role == "owner":
            await message.reply("Бот сейчас не находится на глобальной паузе.")
            return
        await message.reply(RESUME_ALL_NONE)
        return

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Да, возобновить поиск", callback_data="resume_all_confirm")],
            [InlineKeyboardButton(text="Отмена", callback_data="resume_all_cancel")],
        ]
    )
    await message.reply(RESUME_ALL_CONFIRM_TEMPLATE.format(count=paused_count), reply_markup=kb)
