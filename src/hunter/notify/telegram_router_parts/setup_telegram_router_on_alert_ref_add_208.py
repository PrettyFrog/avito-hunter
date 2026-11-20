"""Helper functions for on_alert_ref_add, on_ref_build and feedback (slice 208)."""

from __future__ import annotations

from typing import Any

from aiogram.types import CallbackQuery

from hunter.core.models import User
from hunter.feedback.store import record_feedback
from hunter.notify.messages_ru import CALLBACK_NOT_RECIPIENT


async def handle_alert_ref_add_callback(
    query: CallbackQuery,
    current_user: User,
    store: Any = None,
    settings: Any = None,
) -> None:
    await query.answer("Добавление эталонов устарело в LotRadar v2.", show_alert=True)


async def handle_ref_build_callback(
    query: CallbackQuery,
    current_user: User,
    store: Any = None,
    settings: Any = None,
) -> None:
    await query.answer("Настройка сигнатур эталона устарела в LotRadar v2.", show_alert=True)


async def handle_ref_redo_callback(
    query: CallbackQuery,
    current_user: User,
    ref_build_fn: Any,
    store: Any = None,
) -> None:
    await query.answer("Настройка сигнатур эталона устарела в LotRadar v2.", show_alert=True)


async def handle_ref_confirm_callback(
    query: CallbackQuery,
    current_user: User,
    db_inst: Any,
    owner_chat_id: str,
    store: Any = None,
    settings: Any = None,
) -> None:
    await query.answer("Настройка сигнатур эталона устарела в LotRadar v2.", show_alert=True)


async def handle_ref_cancel_callback(
    query: CallbackQuery,
    db_inst: Any,
    owner_chat_id: str,
) -> None:
    await query.answer("Отменено", show_alert=False)


async def handle_feedback_callback(
    query: CallbackQuery,
    current_user: User,
    database: Any,
    labels: dict[str, str],
) -> None:
    parts = (query.data or "").split(":")
    if len(parts) != 3:
        await query.answer("bad callback")
        return
    _, avito_id, code = parts
    conn = await database.connect()
    if current_user.role != "owner":
        cur = await conn.execute(
            "SELECT 1 FROM deliveries d JOIN events e ON d.event_id = e.id "
            "WHERE e.avito_id = ? AND d.user_id = ? LIMIT 1",
            (avito_id, current_user.id),
        )
        is_recipient = await cur.fetchone() is not None
        if not is_recipient:
            cur_tbl = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='notifications'"
            )
            if await cur_tbl.fetchone():
                cur2 = await conn.execute(
                    "SELECT 1 FROM notifications WHERE avito_id = ? AND (recipient_user_id = ? OR owner_user_id = ?) LIMIT 1",
                    (avito_id, current_user.id, current_user.id),
                )
                is_recipient = await cur2.fetchone() is not None
        if not is_recipient:
            await query.answer(CALLBACK_NOT_RECIPIENT, show_alert=True)
            return

    await record_feedback(database, avito_id, code, owner_user_id=current_user.id)
    label = labels.get(code, code)
    await query.answer(f"Записал: {label}")


async def handle_alert_pause_prod_callback(
    query: CallbackQuery,
    current_user: User,
    store: Any,
    settings: Any,
) -> None:
    await query.answer("Управление товарами v1 устарело. Используйте /my.", show_alert=True)
