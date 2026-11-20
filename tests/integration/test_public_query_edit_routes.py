"""Offline router exercise for physical query edits."""
from urllib.parse import parse_qs, urlparse

import pytest
from aiogram.types import User as TgUser
from tests.unit.test_public_add_flow import _init_env, _make_cb, _make_msg

from hunter.notify.security import AuthAndScopeMiddleware, StrangerLimiter
from hunter.notify.telegram_handlers.public_search_menu import search_token
from hunter.storage.subscriptions import SubscriptionActor


@pytest.mark.asyncio
async def test_price_and_url_edit_through_router(tmp_path):
    db, writer, repo, service, _, router, bot = await _init_env(tmp_path)
    for middleware in router.message.outer_middleware:
        if isinstance(middleware, AuthAndScopeMiddleware):
            middleware.limiter = StrangerLimiter()
    try:
        uid = await db.create_user(telegram_user_id=101, chat_id=101, role="user", status="active")
        actor, tg = SubscriptionActor(uid), TgUser(id=101, is_bot=False, first_name="One")
        await repo.create(actor, "mine", uid, "q1", 10000, name="Телефон")
        for version, price in ((1, 120000), (2, 5000)):
            cb = _make_cb(101, tg, f"sm:edit:{search_token('mine')}:{version}:price", bot)
            await router.propagate_event("callback_query", cb, db=db, bot=bot)
            assert "бюджет" in cb.message.reply.await_args.args[0]
            invalid = _make_msg(101, tg, "-1", bot)
            await router.propagate_event("message", invalid, db=db, bot=bot)
            assert (await repo.get(actor, "mine")).version == version
            value = _make_msg(101, tg, str(price), bot)
            await router.propagate_event("message", value, db=db, bot=bot)
            sub = await repo.get(actor, "mine")
            assert sub.max_price == price and sub.version == version + 1
            assert await db.get_pending_product(101) is None
        cb = _make_cb(101, tg, f"sm:edit:{search_token('mine')}:3:url", bot)
        await router.propagate_event("callback_query", cb, db=db, bot=bot)
        invalid = _make_msg(101, tg, "https://127.0.0.1/", bot)
        await router.propagate_event("message", invalid, db=db, bot=bot)
        assert (await repo.get(actor, "mine")).version == 3
        link = _make_msg(101, tg, "https://avito.ru/sankt-peterburg?q=phone", bot)
        await router.propagate_event("message", link, db=db, bot=bot)
        sub = await repo.get(actor, "mine")
        row = await (await db._conn.execute(
            "SELECT canonical_url FROM queries WHERE id=?", (sub.query_id,)
        )).fetchone()
        assert sub.version == 4 and sub.max_price == 5000
        assert "sankt-peterburg" in row[0]
        assert parse_qs(urlparse(row[0]).query)["pmax"] == ["5000"]
        assert await db.get_pending_product(101) is None
    finally:
        await writer.stop()
        await db.close()


@pytest.mark.asyncio
async def test_stale_handler_keeps_newer_dialog(tmp_path, monkeypatch):
    from hunter.notify.telegram_handlers import public_search_edit
    from hunter.storage.public_edit_dialog import begin_edit, finish_edit

    db, writer, repo, _, _, router, bot = await _init_env(tmp_path)
    for middleware in router.message.outer_middleware:
        if isinstance(middleware, AuthAndScopeMiddleware):
            middleware.limiter = StrangerLimiter()
    try:
        uid = await db.create_user(telegram_user_id=101, chat_id=101, role="user", status="active")
        actor, tg = SubscriptionActor(uid), TgUser(id=101, is_bot=False, first_name="One")
        await repo.create(actor, "mine", uid, "q1", 10000, name="Телефон")
        await begin_edit(repo, actor, 101, "mine", 1, "name")

        async def replaced(*args, **kwargs):
            await begin_edit(repo, actor, 101, "mine", 1, "words")
            await finish_edit(*args, **kwargs)

        monkeypatch.setattr(public_search_edit, "finish_edit", replaced)
        response = _make_msg(101, tg, "Новое имя", bot)
        await router.propagate_event("message", response, db=db, bot=bot)
        state = await db.get_pending_product(101)
        assert state and state["payload"]["field"] == "words"
        assert (await repo.get(actor, "mine")).name == "Телефон"
    finally:
        await writer.stop()
        await db.close()
