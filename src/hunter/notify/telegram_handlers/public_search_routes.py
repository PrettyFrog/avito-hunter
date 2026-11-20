"""Register private search menu routes."""

from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from hunter.core.models import User
from hunter.notify.security import requires
from hunter.notify.telegram_handlers.public_search_controls import register_search_controls
from hunter.notify.telegram_handlers.public_search_edit import cancel_edit, start_edit
from hunter.notify.telegram_handlers.public_search_menu import (
    search_card,
    search_list_page,
    search_token,
)
from hunter.notify.telegram_handlers.public_status import _get_repo
from hunter.storage.subscriptions import SubscriptionActor


def register_search_menu(router: Router, db_resolver: Any) -> None:
    register_search_controls(router, db_resolver)

    @router.message(Command("searches"))
    @requires("user")
    async def list_searches(message: Message, current_user: User | None = None) -> None:
        if current_user is None:
            return
        text, keyboard = await search_list_page(current_user, db_resolver())
        await message.reply(text, parse_mode="HTML", reply_markup=keyboard)

    @router.callback_query(F.data.startswith("sm:"))
    @requires("user")
    async def menu_callback(query: CallbackQuery, current_user: User | None = None) -> None:
        if current_user is None or query.message is None:
            await query.answer("Поиск недоступен.", show_alert=True)
            return
        db = db_resolver()
        parts = (query.data or "").split(":")
        if parts == ["sm", "cancel"]:
            canceled = await cancel_edit(db, current_user, query.message.chat.id)
            await query.answer("Изменение отменено." if canceled else "Изменение не найдено.")
            return
        if len(parts) == 5 and parts[1] == "edit" and parts[3].isdigit():
            subs = await _get_repo(db).list_for_actor(SubscriptionActor(current_user.id))
            matches = [s.id for s in subs if search_token(s.id) == parts[2]]
            if len(matches) == 1:
                await start_edit(query, db, current_user, matches[0], int(parts[3]), parts[4])
                return
        result = None
        if len(parts) == 3 and parts[1] == "page" and parts[2].isdigit():
            await query.answer()
            result = await search_list_page(current_user, db, int(parts[2]))
        elif len(parts) == 3 and parts[1] == "view":
            subs = await _get_repo(db).list_for_actor(SubscriptionActor(current_user.id))
            matches = [s.id for s in subs if search_token(s.id) == parts[2]]
            if len(matches) == 1:
                await query.answer()
                result = await search_card(current_user, db, matches[0])
        if result is None:
            await query.answer("Поиск недоступен.", show_alert=True)
            return
        text, keyboard = result
        await query.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
