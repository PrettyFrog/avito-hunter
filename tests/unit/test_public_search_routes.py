from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from aiogram import Router
from aiogram.types import CallbackQuery, Chat, Message
from aiogram.types import User as TelegramUser
from tests.unit.test_public_status import app_setup as app_setup

from hunter.core.models import User
from hunter.notify.telegram_handlers.public_search_menu import search_token
from hunter.notify.telegram_handlers.public_search_routes import register_search_menu
from hunter.storage.subscriptions import SubscriptionActor


def callback(data):
    telegram_user = TelegramUser(id=2, is_bot=False, first_name="T")
    msg = Message(
        message_id=1,
        date=int(datetime.now(UTC).timestamp()),
        chat=Chat(id=2, type="private"),
        from_user=telegram_user,
    )
    event = CallbackQuery(
        id="cb", from_user=telegram_user, chat_instance="ci", message=msg, data=data
    )
    object.__setattr__(event, "answer", AsyncMock())
    object.__setattr__(msg, "edit_text", AsyncMock())
    return event


@pytest.mark.asyncio
async def test_forged_foreign_search_callback_is_denied(app_setup):
    _, repo, db = app_setup
    await repo.create(SubscriptionActor(1), "secret", 1, "q1", 10000, name="private")
    router = Router()
    register_search_menu(router, lambda: db)
    user = User(id=2, telegram_user_id=2, chat_id=2, role="owner", created_at="")
    event = callback(f"sm:view:{search_token('secret')}")
    await router.propagate_event("callback_query", event, current_user=user)
    event.message.edit_text.assert_not_awaited()
    event.answer.assert_awaited_once_with("Поиск недоступен.", show_alert=True)


@pytest.mark.asyncio
async def test_own_search_button_opens_card(app_setup):
    _, repo, db = app_setup
    await repo.create(SubscriptionActor(1), "mine", 1, "q1", 10000, name="Мой поиск")
    router = Router()
    register_search_menu(router, lambda: db)
    user = User(id=1, telegram_user_id=1, chat_id=1, role="user", created_at="")
    event = callback(f"sm:view:{search_token('mine')}")
    await router.propagate_event("callback_query", event, current_user=user)
    assert "Мой поиск" in event.message.edit_text.await_args.args[0]
    event.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_token_collision_fails_closed(app_setup, monkeypatch):
    import hunter.notify.telegram_handlers.public_search_routes as t930_routes_impl

    _, repo, db = app_setup
    await repo.create(SubscriptionActor(1), "first", 1, "q1", 10000, name="one")
    await repo.create(SubscriptionActor(1), "second", 1, "q2", 10000, name="two")
    monkeypatch.setattr(t930_routes_impl, "search_token", lambda _: "same")
    router = Router()
    register_search_menu(router, lambda: db)
    user = User(id=1, telegram_user_id=1, chat_id=1, role="user", created_at="")
    event = callback("sm:view:same")
    await router.propagate_event("callback_query", event, current_user=user)
    event.message.edit_text.assert_not_awaited()
    event.answer.assert_awaited_once_with("Поиск недоступен.", show_alert=True)
