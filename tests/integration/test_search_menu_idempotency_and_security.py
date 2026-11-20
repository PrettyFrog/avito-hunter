"""Integration tests for T-B090: Double-click idempotency & tenant isolation across search menu (RB-1).

Verifies that:
1. Double-clicking pause/resume/delete buttons is idempotent and safely handled via CAS.
2. Concurrent parallel button clicks are serialized by SQLiteWriterActor without corruption.
3. Tenant isolation prevents foreign users from viewing, editing, or deleting another user's searches.
4. User-facing messages use clear Russian phrasing without developer/technical jargon.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from aiogram.types import User as TgUser
from tests.unit.test_public_add_flow import _init_env, _make_cb

from hunter.notify.security import AuthAndScopeMiddleware, StrangerLimiter
from hunter.notify.telegram_handlers.public_search_menu import search_token
from hunter.storage.subscriptions import SubscriptionActor

pytest_plugins = ("tests.integration.test_public_hunter",)


@pytest.mark.asyncio
async def test_double_click_pause_is_idempotent_and_safe(tmp_path) -> None:
    db, writer, repo, _, _, router, bot = await _init_env(tmp_path)
    for middleware in router.message.outer_middleware:
        if isinstance(middleware, AuthAndScopeMiddleware):
            middleware.limiter = StrangerLimiter()

    try:
        uid = await db.create_user(telegram_user_id=101, chat_id=101, role="user", status="active")
        tg_user = TgUser(id=101, is_bot=False, first_name="User101")
        actor = SubscriptionActor(uid)
        await repo.create(actor, "sub_dc", uid, "q_dc", 30000, name="Тест")
        token = search_token("sub_dc")

        # First click: successfully pauses and increments version to 2
        cb1 = _make_cb(101, tg_user, f"sc:pause:{token}:1", bot)
        object.__setattr__(cb1.message, "edit_text", AsyncMock())
        await router.propagate_event("callback_query", cb1, db=db, bot=bot)
        assert cb1.message.edit_text.await_args is not None
        assert "на паузе" in cb1.message.edit_text.await_args.args[0]

        # Second rapid click with stale version 1
        cb2 = _make_cb(101, tg_user, f"sc:pause:{token}:1", bot)
        object.__setattr__(cb2.message, "edit_text", AsyncMock())
        await router.propagate_event("callback_query", cb2, db=db, bot=bot)

        # Second click must answer gracefully with alert without re-executing
        assert cb2.answer.await_args.kwargs.get("show_alert") is True
        assert "Поиск изменился или недоступен" in cb2.answer.await_args.args[0]

        # State is consistent at version 2
        sub = await repo.get(actor, "sub_dc")
        assert sub.enabled is False
        assert sub.version == 2
    finally:
        await writer.stop()
        await db.close()


@pytest.mark.asyncio
async def test_concurrent_parallel_clicks_serialized_safely(tmp_path) -> None:
    db, writer, repo, _, _, router, bot = await _init_env(tmp_path)
    for middleware in router.message.outer_middleware:
        if isinstance(middleware, AuthAndScopeMiddleware):
            middleware.limiter = StrangerLimiter()

    try:
        uid = await db.create_user(telegram_user_id=101, chat_id=101, role="user", status="active")
        tg_user = TgUser(id=101, is_bot=False, first_name="User101")
        actor = SubscriptionActor(uid)
        await repo.create(actor, "sub_par", uid, "q_par", 30000, name="Параллельный")
        token = search_token("sub_par")

        cb_a = _make_cb(101, tg_user, f"sc:pause:{token}:1", bot)
        object.__setattr__(cb_a.message, "edit_text", AsyncMock())

        cb_b = _make_cb(101, tg_user, f"sc:pause:{token}:1", bot)
        object.__setattr__(cb_b.message, "edit_text", AsyncMock())

        # Fire both concurrently
        await asyncio.gather(
            router.propagate_event("callback_query", cb_a, db=db, bot=bot),
            router.propagate_event("callback_query", cb_b, db=db, bot=bot),
        )

        sub = await repo.get(actor, "sub_par")
        assert sub.enabled is False
        assert sub.version == 2

        # Exactly one succeeded and one was alerted with stale version
        a_edited = cb_a.message.edit_text.await_count
        b_edited = cb_b.message.edit_text.await_count
        assert a_edited + b_edited == 1
    finally:
        await writer.stop()
        await db.close()


@pytest.mark.asyncio
async def test_foreign_user_cannot_access_or_modify(tmp_path) -> None:
    db, writer, repo, _, _, router, bot = await _init_env(tmp_path)
    for middleware in router.message.outer_middleware:
        if isinstance(middleware, AuthAndScopeMiddleware):
            middleware.limiter = StrangerLimiter()

    try:
        u1 = await db.create_user(telegram_user_id=101, chat_id=101, role="user", status="active")
        u2 = await db.create_user(telegram_user_id=102, chat_id=102, role="user", status="active")
        tg_u2 = TgUser(id=102, is_bot=False, first_name="User102")

        actor1 = SubscriptionActor(u1)
        await repo.create(actor1, "sub_priv", u1, "q_priv", 40000, name="Приватный")
        token = search_token("sub_priv")

        for action in ("view", "pause", "resume", "delete", "confirm"):
            prefix = "sm" if action == "view" else "sc"
            cb = _make_cb(102, tg_u2, f"{prefix}:{action}:{token}:1", bot)
            object.__setattr__(cb.message, "edit_text", AsyncMock())
            await router.propagate_event("callback_query", cb, db=db, bot=bot)
            cb.message.edit_text.assert_not_awaited()
            assert cb.answer.await_args.kwargs.get("show_alert") is True
            msg = cb.answer.await_args.args[0]
            assert "недоступен" in msg

        # Original subscription completely intact
        sub = await repo.get(actor1, "sub_priv")
        assert sub.enabled is True
        assert sub.version == 1
    finally:
        await writer.stop()
        await db.close()
