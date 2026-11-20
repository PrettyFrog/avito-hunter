"""Real router lifecycle controls require ownership, versions and confirmation."""
from unittest.mock import AsyncMock

import pytest
from aiogram.types import User as TgUser
from tests.unit.test_public_add_flow import _init_env, _make_cb

from hunter.notify.security import AuthAndScopeMiddleware, StrangerLimiter
from hunter.notify.telegram_handlers.public_search_menu import search_token
from hunter.storage.subscriptions import SubscriptionActor, SubscriptionNotFoundError


async def _click(router, db, bot, tg, action, version):
    cb = _make_cb(tg.id, tg, f"sc:{action}:{search_token('mine')}:{version}", bot)
    object.__setattr__(cb.message, "edit_text", AsyncMock())
    await router.propagate_event("callback_query", cb, db=db, bot=bot)
    return cb


@pytest.mark.asyncio
async def test_lifecycle_menu_guards_versions_and_other_users(tmp_path):
    db, writer, repo, _, _, router, bot = await _init_env(tmp_path)
    for middleware in router.message.outer_middleware:
        if isinstance(middleware, AuthAndScopeMiddleware):
            middleware.limiter = StrangerLimiter()
    try:
        uid = await db.create_user(telegram_user_id=101, chat_id=101, role="user", status="active")
        await db.create_user(telegram_user_id=102, chat_id=102, role="user", status="active")
        own = TgUser(id=101, is_bot=False, first_name="One")
        other = TgUser(id=102, is_bot=False, first_name="Two")
        actor = SubscriptionActor(uid)
        await repo.create(actor, "mine", uid, "q1", 10000, name="Телефон")
        ask = await _click(router, db, bot, own, "delete", 1)
        assert "Удалить" in ask.message.edit_text.await_args.args[0]
        assert (await repo.get(actor, "mine")).enabled
        paused = await _click(router, db, bot, own, "pause", 1)
        assert "на паузе" in paused.message.edit_text.await_args.args[0]
        stale = await _click(router, db, bot, own, "confirm", 1)
        assert stale.answer.await_args.kwargs.get("show_alert")
        assert not (await repo.get(actor, "mine")).enabled
        await _click(router, db, bot, own, "resume", 2)
        assert (await repo.get(actor, "mine")).enabled
        foreign = await _click(router, db, bot, other, "confirm", 3)
        foreign.message.edit_text.assert_not_awaited()
        assert foreign.answer.await_args.kwargs.get("show_alert")
        await _click(router, db, bot, own, "delete", 3)
        removed = await _click(router, db, bot, own, "confirm", 3)
        assert "Ваши поиски (0)" in removed.message.edit_text.await_args.args[0]
        with pytest.raises(SubscriptionNotFoundError):
            await repo.get(actor, "mine")
    finally:
        await writer.stop()
        await db.close()


@pytest.mark.asyncio
async def test_bulk_pause_resume_and_found_for_normal_user(tmp_path, monkeypatch):
    import json
    from tests.unit.test_public_add_flow import _make_cb, _make_msg

    monkeypatch.setattr("hunter.core.product.ProductStore.get_for_user", lambda self, uid: {})
    db, writer, repo, _, _, router, bot = await _init_env(tmp_path)
    try:
        uid = await db.create_user(telegram_user_id=201, chat_id=201, role="user", status="active")
        tg_user = TgUser(id=201, is_bot=False, first_name="Egor")
        actor = SubscriptionActor(uid)

        await repo.create(actor, "s1", uid, "q1", 40000, name="iPhone")
        await repo.create(actor, "s2", uid, "q2", 10000, name="DDR5")

        msg_pause = _make_msg(201, tg_user, "/pause_all", bot)
        await router.propagate_event("message", msg_pause, db=db, bot=bot)
        assert msg_pause.reply.called
        assert "2" in msg_pause.reply.await_args.args[0]

        cb_pause = _make_cb(201, tg_user, "pause_all_confirm", bot)
        object.__setattr__(cb_pause.message, "edit_text", AsyncMock())
        await router.propagate_event("callback_query", cb_pause, db=db, bot=bot)
        assert not (await repo.get(actor, "s1")).enabled
        assert not (await repo.get(actor, "s2")).enabled

        msg_resume = _make_msg(201, tg_user, "/resume_all", bot)
        await router.propagate_event("message", msg_resume, db=db, bot=bot)
        assert msg_resume.reply.called
        assert "2" in msg_resume.reply.await_args.args[0]

        cb_resume = _make_cb(201, tg_user, "resume_all_confirm", bot)
        object.__setattr__(cb_resume.message, "edit_text", AsyncMock())
        await router.propagate_event("callback_query", cb_resume, db=db, bot=bot)
        assert (await repo.get(actor, "s1")).enabled
        assert (await repo.get(actor, "s2")).enabled

        conn = await db.connect()
        await conn.execute(
            "INSERT INTO events (id, avito_id, revision, kind, observed_at, origin) "
            "VALUES ('ev_1', 'av_1', 1, 'NEW_LISTING', '2026-11-16T12:00:00', 'catalog')"
        )
        payload = json.dumps({"title": "iPhone 15 Pro", "price": 40000, "url": "https://avito.ru/item1", "time_ago": "5 мин. назад"})
        await conn.execute(
            "INSERT INTO deliveries (id, event_id, user_id, channel, payload, status, available_at) "
            "VALUES ('del_1', 'ev_1', ?, 'telegram', ?, 'sent', '2026-11-16T12:00:00')",
            (uid, payload),
        )
        await conn.commit()

        msg_found = _make_msg(201, tg_user, "/found", bot)
        await router.propagate_event("message", msg_found, db=db, bot=bot)
        assert msg_found.reply.called
        found_text = msg_found.reply.await_args.args[0]
        assert "iPhone 15 Pro" in found_text
        assert "40 000 ₽" in found_text
    finally:
        await writer.stop()
        await db.close()
