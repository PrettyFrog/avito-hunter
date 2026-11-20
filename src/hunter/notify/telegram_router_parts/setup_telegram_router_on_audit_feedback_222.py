"""Helper functions for on_audit_feedback, on_add_confirm, on_toggle_archive (slice 222)."""

from __future__ import annotations

from typing import Any

from aiogram.types import CallbackQuery, ChatMemberUpdated
from loguru import logger

from hunter.core.db import get_db
from hunter.core.models import User
from hunter.notify.messages_ru import ADD_CANCELLED, BOT_BLOCKED_OWNER_ALERT_TEMPLATE


async def handle_audit_feedback_callback(
    query: CallbackQuery,
    db: Any = None,
) -> None:
    parts = (query.data or "").split(":")
    if len(parts) != 3:
        await query.answer("bad callback")
        return
    _, action, avito_id = parts
    database = db if db is not None else get_db()
    label = "верно отсеяли" if action == "ok" else "зря отсеяли (потерянный лот)"
    await database.record_feedback_entry(avito_id, f"audit_{action}", product_id="audit")
    await query.answer(f"Записано в аудит: {label}")
    if query.message:
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass


async def handle_add_confirm_legacy_callback(
    query: CallbackQuery,
    current_user: User,
    db: Any = None,
    settings: Any = None,
) -> None:
    await query.answer("Мастер добавления v1 устарел. Используйте /add с ссылкой Авито.", show_alert=True)


async def handle_toggle_archive_callback(
    query: CallbackQuery,
    current_user: User,
    render_summary_fn: Any,
    db: Any = None,
) -> None:
    await query.answer("Функция устарела в LotRadar v2.", show_alert=True)


async def handle_add_cancel_callback(
    query: CallbackQuery,
    current_user: User,
    db: Any,
) -> None:
    chat_id = query.message.chat.id if query.message else current_user.chat_id
    await db.delete_pending_product(chat_id)
    if query.message:
        try:
            await query.message.edit_text(ADD_CANCELLED)
        except Exception:
            await query.message.reply(ADD_CANCELLED)
    await query.answer("Отменено")


async def handle_add_edit_callback(
    query: CallbackQuery,
    current_user: User,
    db: Any = None,
) -> None:
    await query.answer("Мастер добавления v1 устарел. Используйте /add с ссылкой Авито.", show_alert=True)


async def _handle_chat_member_blocked(
    user: User,
    database: Any,
    channel: Any,
    store: Any = None,
) -> None:
    logger.warning("User {} blocked/stopped the bot", user.id)
    await database.mark_user_bot_blocked(user.id)
    try:
        conn = await database.connect()
        await conn.execute(
            "UPDATE subscriptions SET enabled = 0 WHERE user_id = ? AND enabled = 1",
            (user.id,),
        )
        await conn.commit()
    except Exception as exc:
        logger.debug("Failed to pause subscriptions for blocked user {}: {}", user.id, exc)
    try:
        p_store = store
        if p_store is None:
            from hunter.core.product import get_product_store
            p_store = get_product_store()
        for p in p_store.get_for_user(user.id).values():
            if p.mode == "live":
                p.mode = "off"
                p.paused_reason = "bot_blocked"
                p_store.save(p)
    except Exception as exc:
        logger.debug("Failed to pause YAML products for blocked user {}: {}", user.id, exc)
    user_repr = f"@{user.username}" if user.username else f"ID {user.id}"
    alert_text = BOT_BLOCKED_OWNER_ALERT_TEMPLATE.format(user_repr=user_repr)
    await channel.alert(alert_text)


async def _handle_chat_member_resumed(
    user: User,
    database: Any,
    store: Any = None,
) -> None:
    db_user = await database.get_user_by_id(user.id)
    if not db_user or db_user.get("status") != "bot_blocked":
        return
    logger.info("User {} unblocked the bot", user.id)
    await database.set_user_status(user.id, "active")
    await database.log_user_event(user.id, "bot_resumed", "Status changed to member")
    try:
        conn = await database.connect()
        await conn.execute(
            "UPDATE subscriptions SET enabled = 1 WHERE user_id = ? AND enabled = 0",
            (user.id,),
        )
        await conn.commit()
    except Exception as exc:
        logger.debug("Failed to resume subscriptions for unblocked user {}: {}", user.id, exc)
    try:
        p_store = store
        if p_store is None:
            from hunter.core.product import get_product_store
            p_store = get_product_store()
        for p in p_store.get_for_user(user.id).values():
            if p.mode == "off" and p.paused_reason == "bot_blocked":
                p.mode = "live"
                p.paused_reason = None
                p_store.save(p)
    except Exception as exc:
        logger.debug("Failed to resume YAML products for unblocked user {}: {}", user.id, exc)


async def handle_my_chat_member_event(
    event: ChatMemberUpdated,
    current_user: User | None,
    database: Any,
    owner_chat_id: str,
    channel: Any,
    store: Any = None,
    settings: Any = None,
) -> None:
    from_user = event.from_user
    if not from_user or str(from_user.id) == owner_chat_id:
        if from_user and str(from_user.id) == owner_chat_id:
            logger.info("Ignoring bot kick/block event for owner {}", from_user.id)
        return

    new_status = event.new_chat_member.status
    if current_user:
        user = current_user
    else:
        user_data = await database.get_user_by_telegram_id(from_user.id)
        user = User(**user_data) if user_data else None
    if not user:
        return

    if new_status in ("kicked", "left"):
        await _handle_chat_member_blocked(user, database, channel, store)
    elif new_status == "member":
        await _handle_chat_member_resumed(user, database, store)
