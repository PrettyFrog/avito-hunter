"""Versioned lifecycle controls for privately owned searches."""
import html
import inspect
from typing import Any

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from hunter.core.models import User
from hunter.notify.security import requires
from hunter.notify.telegram_handlers.public_search_menu import (
    search_card,
    search_list_page,
    search_token,
)
from hunter.notify.telegram_handlers.public_status import _get_repo
from hunter.storage.subscription_controls import set_subscription_enabled
from hunter.storage.subscriptions import (
    NoActiveSlotCapacityError,
    SubscriptionActor,
    SubscriptionError,
)


def _confirmation(sub: Any) -> tuple[str, InlineKeyboardMarkup]:
    token = search_token(sub.id)
    return (
        f"Удалить поиск <b>{html.escape(sub.name or 'Без названия')}</b>?",
        InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="Да, удалить", callback_data=f"sc:confirm:{token}:{sub.version}"
            ),
            InlineKeyboardButton(text="Отмена", callback_data=f"sm:view:{token}"),
        ]]),
    )


async def _apply_control(action: str, repo: Any, actor: SubscriptionActor, sub: Any, db: Any = None) -> None:
    svc = getattr(db, "subscription_service", None) if db else None
    auto_assign = getattr(svc, "auto_assign", False) if svc is not None else False
    cb = getattr(svc, "on_query_assigned", None) if (svc and auto_assign) else None
    cap = getattr(svc, "max_queries_per_slot", 4) if svc else 4
    if action == "confirm":
        await repo.delete(actor, sub.id, expected_version=sub.version)
        if cb:
            refreshed = cb(sub.query_id)
            if inspect.isawaitable(refreshed):
                await refreshed
    else:
        await set_subscription_enabled(
            repo, actor, sub.id, sub.version, action == "resume",
            auto_assign=auto_assign, max_queries_per_slot=cap, on_query_assigned=cb,
        )


def register_search_controls(router: Router, db_resolver: Any) -> None:
    @router.callback_query(F.data.startswith("sc:"))
    @requires("user")
    async def control(query: CallbackQuery, current_user: User | None = None) -> None:
        parts = (query.data or "").split(":")
        if (current_user is None or query.message is None or len(parts) != 4
                or parts[1] not in ("pause", "resume", "delete", "confirm")
                or not parts[3].isdecimal()):
            await query.answer("Поиск недоступен.", show_alert=True)
            return
        db = db_resolver()
        repo, actor = _get_repo(db), SubscriptionActor(current_user.id)
        subs = await repo.list_for_actor(actor)
        matches = [s for s in subs if search_token(s.id) == parts[2]]
        if len(matches) != 1 or matches[0].version != int(parts[3]):
            await query.answer("Поиск изменился или недоступен. Откройте заново.", show_alert=True)
            return
        sub, action = matches[0], parts[1]
        try:
            if action == "delete":
                await query.answer()
                result = _confirmation(sub)
            else:
                await _apply_control(action, repo, actor, sub, db)
                await query.answer()
                result = (
                    await search_list_page(current_user, db) if action == "confirm"
                    else await search_card(current_user, db, sub.id)
                )
        except NoActiveSlotCapacityError:
            await query.answer("Не удалось возобновить: все слоты сейчас заняты.", show_alert=True)
            return
        except SubscriptionError:
            return
        if result is None:
            return
        text, keyboard = result
        await query.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
