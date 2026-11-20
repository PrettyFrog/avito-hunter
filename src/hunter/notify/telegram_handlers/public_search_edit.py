"""Public search name and stop-word edit dialogs."""

import json
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from hunter.application.public_query_edit import finish_query_edit
from hunter.core.models import User
from hunter.notify.telegram_handlers.public_add import handle_public_add_message as handle_add
from hunter.notify.telegram_handlers.public_search_menu import search_card
from hunter.notify.telegram_handlers.public_status import _get_repo
from hunter.storage.public_edit_dialog import begin_edit, finish_edit
from hunter.storage.subscriptions import (
    AccessDeniedError,
    NoActiveSlotCapacityError,
    StaleSubscriptionVersionError,
    SubscriptionActor,
    SubscriptionError,
)


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="sm:cancel")]]
    )


async def cancel_edit(
    db: Any, user: User, chat_id: int, expected_payload: dict[str, object] | None = None
) -> bool:
    repo = _get_repo(db)

    async def tx(conn: Any) -> bool:
        if expected_payload is not None:
            row = await (await conn.execute(
                "SELECT payload FROM pending_products WHERE chat_id=? AND owner_user_id=? "
                "AND step='public_search_edit'", (chat_id, user.id)
            )).fetchone()
            if row is None or json.loads(row[0]) != expected_payload:
                return False
        cursor = await conn.execute(
            "DELETE FROM pending_products WHERE chat_id=? AND owner_user_id=? "
            "AND step='public_search_edit'",
            (chat_id, user.id),
        )
        return cursor.rowcount == 1

    return await repo.writer.submit(tx)


async def start_edit(
    query: Any, db: Any, user: User, sub_id: str, version: int, field: str
) -> None:
    if not query.message or field not in ("name", "words", "keywords", "price", "url"):
        await query.answer("Изменение недоступно.", show_alert=True)
        return
    try:
        await begin_edit(
            _get_repo(db), SubscriptionActor(user.id), query.message.chat.id, sub_id, version, field
        )
    except (SubscriptionError, ValueError):
        await query.answer("Поиск изменился или недоступен. Откройте его заново.", show_alert=True)
        return
    prompt = {
        "name": "Введите новое название поиска:",
        "words": "Введите стоп-слова через запятую или отправьте - для очистки:",
        "keywords": "Введите ключевые слова через запятую или отправьте - для очистки:",
        "price": "Введите новый максимальный бюджет в рублях:",
        "url": "Введите новую ссылку поиска Авито:",
    }[field]
    await query.message.reply(prompt, reply_markup=cancel_keyboard())
    await query.answer()


async def handle_public_add_message(
    message: Any, db: Any, user: User, sub_service: Any = None
) -> bool:
    state = await db.get_pending_product(message.chat.id)
    if not state or state.get("step") != "public_search_edit":
        return await handle_add(message, db, user, sub_service)
    if state.get("owner_user_id") != user.id:
        return False
    text = (message.text or "").strip()
    if text.lower() in ("отмена", "cancel", "/cancel"):
        await cancel_edit(db, user, message.chat.id)
        await message.reply("Изменение отменено.")
        return True
    refreshed = True
    try:
        if state.get("payload", {}).get("field") in ("price", "url"):
            service = sub_service or getattr(db, "subscription_service", None)
            if service is None:
                await message.reply("Редактирование временно недоступно.")
                return True
            _, refreshed = await finish_query_edit(
                service, SubscriptionActor(user.id), message.chat.id, text,
                expected_payload=state["payload"],
            )
        else:
            await finish_edit(
                _get_repo(db), SubscriptionActor(user.id), message.chat.id, text,
                expected_payload=state["payload"],
            )
    except NoActiveSlotCapacityError:
        await message.reply(
            "Нет свободного места для поиска. Попробуйте позже.", reply_markup=cancel_keyboard()
        )
        return True
    except ValueError:
        await message.reply(
            "Некорректное значение. Попробуйте ещё раз.", reply_markup=cancel_keyboard()
        )
        return True
    except (AccessDeniedError, StaleSubscriptionVersionError):
        await cancel_edit(db, user, message.chat.id, expected_payload=state["payload"])
        await message.reply("Поиск изменился или недоступен. Откройте его заново.")
        return True
    except SubscriptionError:
        await message.reply("Этот поиск пока нельзя изменить.", reply_markup=cancel_keyboard())
        return True
    if not refreshed:
        await message.reply(
            "⚠️ Поиск сохранён, но мониторинг не удалось обновить. Обратитесь к владельцу."
        )
    result = await search_card(user, db, state.get("payload", {}).get("sub_id", ""))
    if result is None:
        await message.reply("✅ Поиск обновлён.")
    else:
        text, keyboard = result
        await message.reply(
            f"✅ Поиск обновлён.\n\n{text}", parse_mode="HTML", reply_markup=keyboard
        )
    return True
