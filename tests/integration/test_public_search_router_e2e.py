from unittest.mock import AsyncMock

import pytest
from aiogram.types import User as TgUser
from tests.unit.test_public_add_flow import _init_env, _make_cb, _make_msg

from hunter.notify.security import AuthAndScopeMiddleware, StrangerLimiter
from hunter.notify.telegram_handlers.public_search_menu import search_token
from hunter.storage.subscriptions import SubscriptionActor


@pytest.mark.asyncio
async def test_searches_and_foreign_button_via_real_router(tmp_path):
    db, writer, repo, service, cm, router, bot = await _init_env(tmp_path)
    # A fresh bot scenario owns its limiter; preceding tests must not spend its quota.
    for middleware in router.message.outer_middleware:
        if isinstance(middleware, AuthAndScopeMiddleware):
            middleware.limiter = StrangerLimiter()
    try:
        one = await db.create_user(telegram_user_id=101, chat_id=101, role="user", status="active")
        await db.create_user(telegram_user_id=102, chat_id=102, role="user", status="active")
        await repo.create(SubscriptionActor(one), "sub_mine", one, "q1", 10000, name="Мой поиск")
        u1 = TgUser(id=101, is_bot=False, first_name="One")
        u2 = TgUser(id=102, is_bot=False, first_name="Two")
        message = _make_msg(101, u1, "/searches", bot)
        await router.propagate_event("message", message, db=db, bot=bot)
        assert "Мой поиск" in message.reply.await_args.args[0]
        listing = _make_msg(101, u1, "/list", bot)
        await router.propagate_event("message", listing, db=db, bot=bot)
        assert "Мой поиск" in listing.reply.await_args.args[0]
        assert listing.reply.await_args.kwargs["reply_markup"].inline_keyboard
        own = _make_cb(101, u1, f"sm:view:{search_token('sub_mine')}", bot)
        object.__setattr__(own.message, "edit_text", AsyncMock())
        await router.propagate_event("callback_query", own, db=db, bot=bot)
        assert "Мой поиск" in own.message.edit_text.await_args.args[0]
        await _check_name_and_words(db, repo, router, bot, one, u1)
        pending = _make_cb(101, u1, f"sm:edit:{search_token('sub_mine')}:4:name", bot)
        await router.propagate_event("callback_query", pending, db=db, bot=bot)
        cancel = _make_cb(101, u1, "sm:cancel", bot)
        await router.propagate_event("callback_query", cancel, db=db, bot=bot)
        assert await db.get_pending_product(101) is None
        pending = _make_cb(101, u1, f"sm:edit:{search_token('sub_mine')}:4:name", bot)
        await router.propagate_event("callback_query", pending, db=db, bot=bot)
        cancel_command = _make_msg(101, u1, "/cancel", bot)
        await router.propagate_event("message", cancel_command, db=db, bot=bot)
        assert await db.get_pending_product(101) is None
        assert "отменено" in cancel_command.reply.await_args.args[0]
        foreign_edit = _make_cb(102, u2, f"sm:edit:{search_token('sub_mine')}:4:name", bot)
        await router.propagate_event("callback_query", foreign_edit, db=db, bot=bot)
        assert foreign_edit.answer.await_args.kwargs.get("show_alert")
        assert await db.get_pending_product(102) is None
        foreign = _make_cb(102, u2, f"sm:view:{search_token('sub_mine')}", bot)
        object.__setattr__(foreign.message, "edit_text", AsyncMock())
        await router.propagate_event("callback_query", foreign, db=db, bot=bot)
        foreign.message.edit_text.assert_not_awaited()
        foreign.answer.assert_awaited_once_with("Поиск недоступен.", show_alert=True)
    finally:
        await writer.stop()
        await db.close()


async def _check_name_and_words(db, repo, router, bot, one, u1):
    rename = _make_cb(101, u1, f"sm:edit:{search_token('sub_mine')}:1:name", bot)
    await router.propagate_event("callback_query", rename, db=db, bot=bot)
    assert "новое название" in rename.message.reply.await_args.args[0]
    response = _make_msg(101, u1, "Новое имя", bot)
    await router.propagate_event("message", response, db=db, bot=bot)
    assert "обновлён" in response.reply.await_args.args[0]
    assert (await repo.get(SubscriptionActor(one), "sub_mine")).name == "Новое имя"
    stale = _make_cb(101, u1, f"sm:edit:{search_token('sub_mine')}:1:name", bot)
    await router.propagate_event("callback_query", stale, db=db, bot=bot)
    assert stale.answer.await_args.kwargs.get("show_alert")
    words = _make_cb(101, u1, f"sm:edit:{search_token('sub_mine')}:2:words", bot)
    await router.propagate_event("callback_query", words, db=db, bot=bot)
    value = _make_msg(101, u1, "Битый, запчасти", bot)
    await router.propagate_event("message", value, db=db, bot=bot)
    assert (await repo.get(SubscriptionActor(one), "sub_mine")).manual_stopwords == "битый,запчасти"
    words = _make_cb(101, u1, f"sm:edit:{search_token('sub_mine')}:3:words", bot)
    await router.propagate_event("callback_query", words, db=db, bot=bot)
    clear = _make_msg(101, u1, "-", bot)
    await router.propagate_event("message", clear, db=db, bot=bot)
    assert (await repo.get(SubscriptionActor(one), "sub_mine")).manual_stopwords == ""
