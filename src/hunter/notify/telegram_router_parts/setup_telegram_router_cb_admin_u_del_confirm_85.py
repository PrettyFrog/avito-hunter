"""Telegram router parts: admin user delete confirm and invite management (T-367)."""

from __future__ import annotations

import hashlib
import os
import secrets
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from hunter.core.config import get_settings
from hunter.core.models import now_utc
from hunter.core.product import delete_user_products
from hunter.notify.messages_ru import (
    ADMIN_BTN_BACK,
    ADMIN_CONFIRM_EXPIRED,
    BROADCAST_PROMPT,
    INVITE_CREATED_TEMPLATE,
    INVITE_FORWARD_TEMPLATE,
    clean_name,
)


async def handle_admin_u_del_confirm_callback(
    query: CallbackQuery,
    current_user: Any,
    db_inst: Any,
    store: Any,
    admin_confirmations: dict[tuple[int, int], dict[str, Any]],
    render_users_list_fn: Any,
) -> None:
    """Handle admin:u_del_confirm: callback deleting user data and refreshing list."""
    msg = query.message
    if not msg:
        return
    key = (msg.chat.id, msg.message_id)
    conf = admin_confirmations.pop(key, None)
    uid = int((query.data or "").split(":", 2)[2])
    if not conf or conf.get("action") != "del" or conf.get("user_id") != uid or conf.get("expires_at", 0) < time.time():
        await query.answer(ADMIN_CONFIRM_EXPIRED, show_alert=True)
        return
    await delete_user_products(uid, store=store, db=db_inst)
    await db_inst.delete_user_data(uid)
    await query.answer("Пользователь и его данные полностью удалены.", show_alert=True)
    await render_users_list_fn(msg, db_inst)


async def handle_admin_invites_callback(
    query: CallbackQuery,
    current_user: Any,
    db_inst: Any,
) -> None:
    """Handle admin:invites callback rendering list of active invites."""
    invites = await db_inst.get_active_invites(current_user.id)
    kb_rows = [
        [
            InlineKeyboardButton(text="➕ Создать (Друг)", callback_data="admin:inv_create:friend"),
            InlineKeyboardButton(text="➕ Создать (Семья)", callback_data="admin:inv_create:family"),
        ]
    ]
    text_lines = ["🎫 <b>Активные приглашения:</b>\n"]
    if not invites:
        text_lines.append("Нет активных приглашений.")
    else:
        for inv in invites:
            label = clean_name(inv.get("label") or "Без имени")
            tier_ru = "Семья" if inv["tier"] == "family" else "Друг"
            exp = inv.get("expires_at", "")[:10]
            text_lines.append(f"• <b>{label}</b> ({tier_ru}, до {exp})")
            kb_rows.append([InlineKeyboardButton(text=f"❌ Отозвать {label}", callback_data=f"admin:inv_revoke:{inv['id']}")])
    kb_rows.append([InlineKeyboardButton(text=ADMIN_BTN_BACK, callback_data="admin:root")])
    if query.message:
        await query.message.edit_text("\n".join(text_lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows), parse_mode="HTML")
    await query.answer()


async def handle_admin_inv_create_callback(
    query: CallbackQuery,
    current_user: Any,
    db_inst: Any,
    channel_bot: Any = None,
) -> None:
    """Handle admin:inv_create: callback generating a new invite link."""
    tier = (query.data or "").split(":", 2)[2]
    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    expires_at = (now_utc() + timedelta(hours=48)).isoformat()
    tier_ru = "Семья" if tier == "family" else "Друг"
    label = f"Admin {tier_ru}"
    await db_inst.create_invite(
        token_hash=token_hash,
        label=label,
        tier=tier,
        created_by=current_user.id,
        expires_at=expires_at,
    )
    bot_username = "avito_hunter_bot"
    bot = query.bot or channel_bot
    if bot:
        try:
            me = await bot.get_me()
            if me and me.username:
                bot_username = me.username
        except Exception:
            pass
    link = f"https://t.me/{bot_username}?start=inv_{raw_token}"
    fwd_text = INVITE_FORWARD_TEMPLATE.format(link=link)
    inv_text = INVITE_CREATED_TEMPLATE.format(name=label, tier_ru=tier_ru, link=link, forward_text=fwd_text)
    if query.message:
        await query.message.reply(inv_text, parse_mode="HTML")
    await query.answer("Приглашение создано!")
    await handle_admin_invites_callback(query, current_user, db_inst)


async def handle_admin_inv_revoke_callback(
    query: CallbackQuery,
    current_user: Any,
    db_inst: Any,
) -> None:
    """Handle admin:inv_revoke: callback revoking an invite by id."""
    parts = (query.data or "").split(":", 2)
    if len(parts) < 3:
        return
    inv_id = int(parts[2])
    ok = await db_inst.revoke_invite(inv_id)
    if ok:
        await query.answer("Приглашение отозвано.", show_alert=True)
    else:
        await query.answer("Приглашение уже отозвано или не найдено.", show_alert=True)
    await handle_admin_invites_callback(query, current_user, db_inst)


async def handle_admin_broadcast_callback(
    query: CallbackQuery,
    current_user: Any,
    db_inst: Any,
) -> None:
    """Handle admin:broadcast callback setting await_broadcast_text state."""
    chat_id = query.message.chat.id if query.message else current_user.chat_id
    await db_inst.save_pending_product(chat_id, "await_broadcast_text", {}, ttl_hours=2)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=ADMIN_BTN_BACK, callback_data="admin:root")]]
    )
    if query.message:
        await query.message.edit_text(BROADCAST_PROMPT, reply_markup=kb)
    await query.answer()


async def _get_admin_product_counts(conn: Any, store: Any) -> tuple[int, int, int, int]:
    """Return (live_p, shadow_p, off_p, total_p) for admin stats."""
    settings = get_settings()
    is_v2 = getattr(getattr(settings, "public_v2", None), "enabled", False)
    if is_v2:
        cur = await conn.execute(
            "SELECT count(*), "
            "coalesce(sum(case when enabled = 1 then 1 else 0 end), 0), "
            "coalesce(sum(case when enabled = 0 then 1 else 0 end), 0) "
            "FROM subscriptions"
        )
        s_row = await cur.fetchone()
        tot, live, off = (s_row[0], s_row[1], s_row[2]) if s_row else (0, 0, 0)
        return live, 0, off, tot
    prods_dict, _ = store.load_all()
    prods = list(prods_dict.values())
    live = sum(1 for p in prods if p.mode == "live")
    shadow = sum(1 for p in prods if p.mode == "shadow")
    off = sum(1 for p in prods if p.mode == "off")
    return live, shadow, off, len(prods)


async def handle_admin_stats_callback(
    query: CallbackQuery,
    db_inst: Any,
    store: Any,
) -> None:
    """Handle admin:stats callback showing service statistics."""
    conn = await db_inst.connect()
    cur = await conn.execute("SELECT status, count(*) FROM users GROUP BY status")
    u_counts = dict(await cur.fetchall())
    active_u = u_counts.get("active", 0)
    bot_blocked_u = u_counts.get("bot_blocked", 0)
    owner_blocked_u = u_counts.get("blocked_by_owner", 0)

    live_p, shadow_p, off_p, total_p = await _get_admin_product_counts(conn, store)

    cur = await conn.execute("SELECT count(*) FROM support_tickets WHERE status = 'open'")
    open_tickets = (await cur.fetchone())[0]

    cur = await conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('notifications', 'deliveries')")
    found_tbls = {r[0] for r in await cur.fetchall()}
    if "deliveries" in found_tbls:
        cur = await conn.execute("SELECT count(*) FROM deliveries WHERE status = 'sent' AND datetime(created_at) >= datetime('now', '-24 hours')")
        notifs_24h = (await cur.fetchone())[0]
    elif "notifications" in found_tbls:
        cur = await conn.execute("SELECT count(*) FROM notifications WHERE datetime(created_at) >= datetime('now', '-24 hours')")
        notifs_24h = (await cur.fetchone())[0]
    else:
        notifs_24h = 0

    text = (
        "📊 <b>Статистика сервиса:</b>\n\n"
        f"👥 <b>Пользователи:</b>\n"
        f"• Активных: {active_u}\n"
        f"• Заблокировали бота: {bot_blocked_u}\n"
        f"• Заблокированы владельцем: {owner_blocked_u}\n\n"
        f"📦 <b>Товары:</b>\n"
        f"• Live: {live_p} | Shadow: {shadow_p} | Пауза: {off_p} | Всего: {total_p}\n\n"
        f"🎫 <b>Поддержка:</b>\n"
        f"• Открытых обращений: {open_tickets}\n\n"
        f"🔔 <b>Уведомления:</b>\n"
        f"• Доставлено за 24ч: {notifs_24h}"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=ADMIN_BTN_BACK, callback_data="admin:root")]]
    )
    if query.message:
        await query.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await query.answer()


async def handle_admin_system_callback(
    query: CallbackQuery,
    db_inst: Any,
) -> None:
    """Handle admin:system callback showing system health and resource status."""
    is_paused = await db_inst.is_paused()
    captcha_val = await db_inst.get_runtime("captcha", "0")
    breaker_val = await db_inst.get_runtime("circuit_breaker_active", "0")
    settings = get_settings()
    data_dir = Path(os.environ.get("HUNTER_DATA_DIR") or settings.data_dir)
    flag_pause = (data_dir / "pause.flag").exists()
    flag_captcha = (data_dir / "captcha.flag").exists()

    pause_status = "🔴 Включена" if (is_paused or flag_pause) else "🟢 Выключена (работает)"
    captcha_status = "⚠️ Обнаружена" if (captcha_val == "1" or flag_captcha) else "🟢 Чисто"
    breaker_status = "⚠️ Активен (авто-торможение)" if breaker_val == "1" else "🟢 В норме"

    conn = await db_inst.connect()
    cur = await conn.execute("SELECT count(*) FROM jobs WHERE status = 'pending'")
    pending_jobs = (await cur.fetchone())[0]

    import resource
    rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024

    text = (
        "⚙️ <b>Состояние системы:</b>\n\n"
        f"• Глобальная пауза: {pause_status}\n"
        f"• Капча: {captcha_status}\n"
        f"• Защита бюджета (circuit breaker): {breaker_status}\n"
        f"• Очередь фоновых задач (jobs): {pending_jobs}\n"
        f"• Память процесса (RSS): {rss_mb:.1f} МБ"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=ADMIN_BTN_BACK, callback_data="admin:root")]]
    )
    if query.message:
        await query.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await query.answer()
