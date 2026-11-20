"""Telegram router parts: admin user pause/resume/block handlers (T-365)."""

from __future__ import annotations

import time
from typing import Any

from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from loguru import logger

from hunter.core.config import TierLimitConfig, get_settings
from hunter.core.models import now_utc
from hunter.core.product import (
    apply_product_pause,
    clear_product_pause,
    save_product_profile,
    stretch_user_product_intervals,
)
from hunter.notify.messages_ru import (
    ADMIN_BLOCK_CONFIRM_TEMPLATE,
    ADMIN_CONFIRM_EXPIRED,
    ADMIN_DELETE_CONFIRM_TEMPLATE,
    ADMIN_TIER_CHANGED_USER_NOTIFY_TEMPLATE,
    USER_BLOCKED_BY_ADMIN,
    clean_name,
)


async def handle_admin_u_pause_callback(
    query: CallbackQuery,
    current_user: Any,
    db_inst: Any,
    store: Any,
    render_user_view_fn: Any,
) -> None:
    """Handle admin:u_pause: callback, pausing all user products."""
    uid = int((query.data or "").split(":", 2)[2])
    settings = get_settings()
    is_v2 = getattr(getattr(settings, "public_v2", None), "enabled", False)
    if is_v2:
        conn = await db_inst.connect()
        cur = await conn.execute(
            "UPDATE subscriptions SET enabled = 0, updated_at = ? WHERE user_id = ? AND enabled = 1",
            (now_utc().isoformat(), uid),
        )
        count = cur.rowcount if cur.rowcount >= 0 else 0
        if hasattr(conn, "commit"):
            await conn.commit()
    else:
        prods = store.get_for_user(uid)
        count = 0
        for p in prods.values():
            if apply_product_pause(p, "owner_paused", actor_role="owner"):
                save_product_profile(
                    p, directory=getattr(store, "directory", settings.data_dir / "products")
                )
                count += 1
    await query.answer(f"Поставлено на паузу товаров: {count}", show_alert=True)
    await render_user_view_fn(query.message, uid, current_user, db_inst)


async def handle_admin_u_resume_callback(
    query: CallbackQuery,
    current_user: Any,
    db_inst: Any,
    store: Any,
    render_user_view_fn: Any,
) -> None:
    """Handle admin:u_resume: callback, resuming all user products."""
    uid = int((query.data or "").split(":", 2)[2])
    settings = get_settings()
    is_v2 = getattr(getattr(settings, "public_v2", None), "enabled", False)
    if is_v2:
        conn = await db_inst.connect()
        cur = await conn.execute(
            "UPDATE subscriptions SET enabled = 1, updated_at = ? WHERE user_id = ? AND enabled = 0",
            (now_utc().isoformat(), uid),
        )
        count = cur.rowcount if cur.rowcount >= 0 else 0
        if hasattr(conn, "commit"):
            await conn.commit()
    else:
        prods = store.get_for_user(uid)
        count = 0
        for p in prods.values():
            ok, _ = clear_product_pause(p, actor_role="owner")
            if ok:
                save_product_profile(
                    p, directory=getattr(store, "directory", settings.data_dir / "products")
                )
                count += 1
    await query.answer(f"Возобновлено товаров: {count}", show_alert=True)
    await render_user_view_fn(query.message, uid, current_user, db_inst)


async def handle_admin_u_block_callback(
    query: CallbackQuery,
    current_user: Any,
    db_inst: Any,
    admin_confirmations: dict[tuple[int, int], dict[str, Any]],
) -> None:
    """Handle admin:u_block: callback showing block confirmation prompt."""
    uid = int((query.data or "").split(":", 2)[2])
    if uid == current_user.id:
        await query.answer("Нельзя заблокировать самого себя!", show_alert=True)
        return
    u = await db_inst.get_user_by_id(uid)
    if not u:
        await query.answer("Пользователь не найден", show_alert=True)
        return
    msg = query.message
    if not msg:
        return
    key = (msg.chat.id, msg.message_id)
    admin_confirmations[key] = {
        "action": "block",
        "user_id": uid,
        "expires_at": time.time() + 120.0,
    }
    uname = clean_name(u.get("display_name") or u.get("username") or str(uid))
    text = ADMIN_BLOCK_CONFIRM_TEMPLATE.format(name=uname, id=uid)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Да, заблокировать", callback_data=f"admin:u_block_confirm:{uid}"
                )
            ],
            [InlineKeyboardButton(text="Отмена", callback_data=f"admin:u_view:{uid}")],
        ]
    )
    await msg.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await query.answer()


async def handle_admin_u_block_confirm_callback(
    query: CallbackQuery,
    current_user: Any,
    db_inst: Any,
    store: Any,
    admin_confirmations: dict[tuple[int, int], dict[str, Any]],
    render_user_view_fn: Any,
) -> None:
    """Handle admin:u_block_confirm: callback, executing block and pausing products."""
    msg = query.message
    if not msg:
        return
    key = (msg.chat.id, msg.message_id)
    conf = admin_confirmations.pop(key, None)
    uid = int((query.data or "").split(":", 2)[2])
    if (
        not conf
        or conf.get("action") != "block"
        or conf.get("user_id") != uid
        or conf.get("expires_at", 0) < time.time()
    ):
        await query.answer(ADMIN_CONFIRM_EXPIRED, show_alert=True)
        return
    await db_inst.block_user_by_owner(uid)
    settings = get_settings()
    is_v2 = getattr(getattr(settings, "public_v2", None), "enabled", False)
    if is_v2:
        conn = await db_inst.connect()
        await conn.execute(
            "UPDATE subscriptions SET enabled = 0, updated_at = ? WHERE user_id = ? AND enabled = 1",
            (now_utc().isoformat(), uid),
        )
        if hasattr(conn, "commit"):
            await conn.commit()
    else:
        for p in store.get_for_user(uid).values():
            if apply_product_pause(p, "owner_paused", actor_role="owner"):
                save_product_profile(
                    p, directory=getattr(store, "directory", settings.data_dir / "products")
                )
    u = await db_inst.get_user_by_id(uid)
    if u and u.get("chat_id") and query.bot:
        try:
            await query.bot.send_message(chat_id=u["chat_id"], text=USER_BLOCKED_BY_ADMIN)
        except Exception as exc:
            logger.debug("Failed notifying user about block: {}", exc)
    await query.answer("Пользователь заблокирован.", show_alert=True)
    await render_user_view_fn(msg, uid, current_user, db_inst)


async def handle_admin_u_unblock_callback(
    query: CallbackQuery,
    current_user: Any,
    db_inst: Any,
    render_user_view_fn: Any,
) -> None:
    """Handle admin:u_unblock: callback unblocking a user."""
    uid = int((query.data or "").split(":", 2)[2])
    await db_inst.unblock_user_by_owner(uid)
    await query.answer("Пользователь разблокирован.", show_alert=True)
    await render_user_view_fn(query.message, uid, current_user, db_inst)


async def handle_admin_u_tier_callback(
    query: CallbackQuery,
    current_user: Any,
) -> None:
    """Handle admin:u_tier: callback showing tier selection keyboard."""
    uid = int((query.data or "").split(":", 2)[2])
    if uid == current_user.id:
        await query.answer("Нельзя менять тариф владельца!", show_alert=True)
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="👥 Друг (friend)", callback_data=f"admin:u_tier_set:{uid}:friend"
                )
            ],
            [
                InlineKeyboardButton(
                    text="👨‍👩‍👧‍👦 Семья (family)", callback_data=f"admin:u_tier_set:{uid}:family"
                )
            ],
            [InlineKeyboardButton(text="◀️ Назад", callback_data=f"admin:u_view:{uid}")],
        ]
    )
    if query.message:
        await query.message.edit_text(
            f"Выберите новый тариф для пользователя ID {uid}:", reply_markup=kb
        )
    await query.answer()


async def handle_admin_u_tier_set_callback(
    query: CallbackQuery,
    current_user: Any,
    db_inst: Any,
    store: Any,
    render_user_view_fn: Any,
) -> None:
    """Handle admin:u_tier_set: callback updating user tier and intervals."""
    parts = (query.data or "").split(":")
    if len(parts) != 4:
        await query.answer("Неверный callback", show_alert=True)
        return
    uid = int(parts[2])
    new_tier = parts[3]
    settings = get_settings()
    multiuser_cfg = getattr(settings, "multiuser", None)
    tiers_dict = getattr(multiuser_cfg, "tiers", {}) if multiuser_cfg else {}
    tier_cfg = tiers_dict.get(new_tier, TierLimitConfig())
    await db_inst.change_user_tier(uid, new_tier)
    stretch_user_product_intervals(uid, tier_cfg.min_interval_minutes, store=store)
    u = await db_inst.get_user_by_id(uid)
    if u and u.get("chat_id") and query.bot:
        tier_ru = {"family": "Семья", "friend": "Друг"}.get(new_tier, new_tier)
        try:
            await query.bot.send_message(
                chat_id=u["chat_id"],
                text=ADMIN_TIER_CHANGED_USER_NOTIFY_TEMPLATE.format(tier_ru=tier_ru),
            )
        except Exception as exc:
            logger.debug("Failed notifying user about tier change: {}", exc)
    await query.answer(f"Тариф изменён на {new_tier}!", show_alert=True)
    await render_user_view_fn(query.message, uid, current_user, db_inst)


async def handle_admin_u_del_callback(
    query: CallbackQuery,
    current_user: Any,
    db_inst: Any,
    admin_confirmations: dict[tuple[int, int], dict[str, Any]],
) -> None:
    """Handle admin:u_del: callback showing delete confirmation prompt."""
    uid = int((query.data or "").split(":", 2)[2])
    if uid == current_user.id:
        await query.answer("Нельзя удалить владельца!", show_alert=True)
        return
    u = await db_inst.get_user_by_id(uid)
    if not u:
        await query.answer("Пользователь не найден", show_alert=True)
        return
    msg = query.message
    if not msg:
        return
    key = (msg.chat.id, msg.message_id)
    admin_confirmations[key] = {
        "action": "del",
        "user_id": uid,
        "expires_at": time.time() + 120.0,
    }
    uname = clean_name(u.get("display_name") or u.get("username") or str(uid))
    text = ADMIN_DELETE_CONFIRM_TEMPLATE.format(name=uname, id=uid)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Да, удалить навсегда", callback_data=f"admin:u_del_confirm:{uid}"
                )
            ],
            [InlineKeyboardButton(text="Отмена", callback_data=f"admin:u_view:{uid}")],
        ]
    )
    await msg.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await query.answer()
