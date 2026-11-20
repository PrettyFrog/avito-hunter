"""Telegram router parts: cmd_delete_me and cmd_admin helpers (T-363)."""

from __future__ import annotations

from typing import Any

from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from loguru import logger

from hunter.core.config import TierLimitConfig, get_settings
from hunter.core.product import delete_user_products
from hunter.notify.messages_ru import (
    ADMIN_BTN_BACK,
    ADMIN_BTN_BROADCAST,
    ADMIN_BTN_ERRORS,
    ADMIN_BTN_INVITES,
    ADMIN_BTN_STATS,
    ADMIN_BTN_SYSTEM,
    ADMIN_BTN_U_BLOCK,
    ADMIN_BTN_U_DELETE,
    ADMIN_BTN_U_PAUSE,
    ADMIN_BTN_U_RESUME,
    ADMIN_BTN_U_TIER,
    ADMIN_BTN_U_UNBLOCK,
    ADMIN_BTN_USERS,
    ADMIN_MENU_TITLE,
    ADMIN_USER_DETAIL_TEMPLATE,
    DELETE_ME_BTN_CANCEL,
    DELETE_ME_BTN_CONFIRM,
    DELETE_ME_CANCELLED,
    DELETE_ME_CONFIRM,
    DELETE_ME_OWNER_ALERT_TEMPLATE,
    DELETE_ME_OWNER_FORBIDDEN,
    DELETE_ME_SUCCESS,
    clean_name,
)


def admin_root_keyboard() -> InlineKeyboardMarkup:
    """Return inline keyboard for root admin menu."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=ADMIN_BTN_USERS, callback_data="admin:users")],
            [InlineKeyboardButton(text=ADMIN_BTN_INVITES, callback_data="admin:invites")],
            [InlineKeyboardButton(text=ADMIN_BTN_BROADCAST, callback_data="admin:broadcast")],
            [InlineKeyboardButton(text=ADMIN_BTN_STATS, callback_data="admin:stats")],
            [InlineKeyboardButton(text=ADMIN_BTN_SYSTEM, callback_data="admin:system")],
            [InlineKeyboardButton(text=ADMIN_BTN_ERRORS, callback_data="admin:errors:0")],
        ]
    )


async def handle_delete_me_command(
    message: Message,
    current_user: Any,
    safe_reply_fn: Any,
) -> None:
    """Handle /delete_me command showing confirmation or owner forbidden."""
    if current_user.role == "owner":
        await safe_reply_fn(message, DELETE_ME_OWNER_FORBIDDEN)
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=DELETE_ME_BTN_CONFIRM, callback_data="delme:confirm")],
            [InlineKeyboardButton(text=DELETE_ME_BTN_CANCEL, callback_data="delme:cancel")],
        ]
    )
    await safe_reply_fn(message, DELETE_ME_CONFIRM, reply_markup=kb, parse_mode="HTML")


async def handle_delete_me_confirm_callback(
    query: CallbackQuery,
    current_user: Any,
    db_inst: Any,
    store: Any,
) -> None:
    """Handle delme:confirm callback executing full user deletion and owner alert."""
    if current_user.role == "owner":
        await query.answer(DELETE_ME_OWNER_FORBIDDEN, show_alert=True)
        return
    await delete_user_products(current_user.id, store=store, db=db_inst)
    await db_inst.delete_user_data(current_user.id)
    await query.answer()
    if query.message:
        await query.message.edit_text(DELETE_ME_SUCCESS)
    owner = await db_inst.get_owner_user()
    if owner and owner.get("chat_id") and query.bot:
        user_repr = f"@{current_user.username}" if current_user.username else f"ID {current_user.id}"
        alert_text = DELETE_ME_OWNER_ALERT_TEMPLATE.format(user_repr=user_repr)
        try:
            await query.bot.send_message(chat_id=owner["chat_id"], text=alert_text)
        except Exception as exc:
            logger.debug("Failed notifying owner about /delete_me: {}", exc)


async def handle_delete_me_cancel_callback(query: CallbackQuery) -> None:
    """Handle delme:cancel callback."""
    await query.answer("Отменено")
    if query.message:
        await query.message.edit_text(DELETE_ME_CANCELLED)


async def handle_admin_command(message: Message, safe_reply_fn: Any) -> None:
    """Handle /admin command showing root admin menu."""
    await safe_reply_fn(message, ADMIN_MENU_TITLE, reply_markup=admin_root_keyboard(), parse_mode="HTML")


async def handle_admin_root_callback(query: CallbackQuery) -> None:
    """Render root admin menu on callback query."""
    if query.message:
        await query.message.edit_text(ADMIN_MENU_TITLE, reply_markup=admin_root_keyboard(), parse_mode="HTML")
    await query.answer()


async def _count_user_products(db_inst: Any, uid: int, store: Any = None) -> int:
    """Count active products/subscriptions for a user."""
    try:
        conn = await db_inst.connect()
        cur = await conn.execute("SELECT count(*) FROM subscriptions WHERE user_id = ?", (uid,))
        row = await cur.fetchone()
        if row is not None:
            return int(row[0])
    except Exception as exc:
        logger.debug("Failed querying user subscriptions count: {}", exc)
    if store is not None and hasattr(store, "get_for_user"):
        return len(store.get_for_user(uid))
    return 0


async def render_admin_users_list(msg: Any, db_inst: Any, store: Any = None) -> None:
    """Fetch users and edit message with user list keyboard."""
    if store is None:
        from hunter.core.product import get_product_store
        store = get_product_store()
    conn = await db_inst.connect()
    cur = await conn.execute(
        'SELECT id, username, display_name, role, tier, status FROM users WHERE '
            "status != 'deleted' ORDER BY id ASC"
    )
    users = await cur.fetchall()
    kb_rows = []
    for u in users:
        uid = u[0]
        uname = clean_name(u[2] or (f"@{u[1]}" if u[1] else f"ID {uid}"))
        role = u[3]
        u_prods = await _count_user_products(db_inst, uid, store)
        btn_txt = f"{uname} ({role}, {u_prods} тов.)"
        kb_rows.append([InlineKeyboardButton(text=btn_txt, callback_data=f"admin:u_view:{uid}")])
    kb_rows.append([InlineKeyboardButton(text=ADMIN_BTN_BACK, callback_data="admin:root")])
    if msg:
        await msg.edit_text("👥 <b>Список пользователей:</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows), parse_mode="HTML")


async def render_admin_user_view(
    msg: Any,
    uid: int,
    current_user: Any,
    db_inst: Any,
    store: Any = None,
) -> None:
    """Render detailed view of a user with action buttons."""
    u = await db_inst.get_user_by_id(uid)
    if not u or u.get("status") == "deleted":
        if msg:
            await msg.edit_text("Пользователь не найден или удалён.")
        return
    if store is None:
        from hunter.core.product import get_product_store
        store = get_product_store()
    settings = get_settings()
    user_repr = f"@{u['username']}" if u.get("username") else f"ID {uid}"
    name = clean_name(u.get("display_name") or u.get("username") or str(uid))
    status_map = {
        "active": "🟢 Активен",
        "blocked_by_owner": "🚫 Заблокирован владельцем",
        "bot_blocked": "🛑 Заблокировал бота",
    }
    tier_map = {"family": "Семья", "friend": "Друг"}
    multiuser_cfg = getattr(settings, "multiuser", None)
    tiers_dict = getattr(multiuser_cfg, "tiers", {}) if multiuser_cfg else {}
    tier_cfg = tiers_dict.get(u.get("tier", "friend"), TierLimitConfig())
    products_count = await _count_user_products(db_inst, uid, store)
    text = ADMIN_USER_DETAIL_TEMPLATE.format(
        name=name,
        user_repr=user_repr,
        id=uid,
        status_ru=status_map.get(u.get("status", ""), u.get("status", "")),
        tier_ru=tier_map.get(u.get("tier", ""), u.get("tier", "")),
        products_count=products_count,
        max_products=tier_cfg.max_products,
        min_interval=tier_cfg.min_interval_minutes,
        max_alerts=tier_cfg.max_alerts_per_hour,
    )
    kb_rows = [
        [
            InlineKeyboardButton(text=ADMIN_BTN_U_PAUSE, callback_data=f"admin:u_pause:{uid}"),
            InlineKeyboardButton(text=ADMIN_BTN_U_RESUME, callback_data=f"admin:u_resume:{uid}"),
        ]
    ]
    if uid != current_user.id:
        if u.get("status") == "blocked_by_owner":
            block_btn = InlineKeyboardButton(text=ADMIN_BTN_U_UNBLOCK, callback_data=f"admin:u_unblock:{uid}")
        else:
            block_btn = InlineKeyboardButton(text=ADMIN_BTN_U_BLOCK, callback_data=f"admin:u_block:{uid}")
        kb_rows.append([block_btn])
        kb_rows.append([InlineKeyboardButton(text=ADMIN_BTN_U_TIER, callback_data=f"admin:u_tier:{uid}")])
        kb_rows.append([InlineKeyboardButton(text=ADMIN_BTN_U_DELETE, callback_data=f"admin:u_del:{uid}")])
    kb_rows.append([InlineKeyboardButton(text="◀️ К списку пользователей", callback_data="admin:users")])
    if msg:
        await msg.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows), parse_mode="HTML")

