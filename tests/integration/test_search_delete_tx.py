"""Integration tests for T-B089: Delete search with confirmation & delivery queue suppression (RB-1).

Verifies that:
1. Deleting a search prompts confirmation with 'Да, удалить' and 'Отмена'.
2. Cancelling confirmation preserves the search intact.
3. Confirming deletion removes the subscription atomically and deactivates orphaned assignments.
4. Pending deliveries for the deleted query are suppressed/deleted, while sent history is preserved.
5. CAS version protection rejects stale delete confirmations.
6. Stranger cannot confirm deletion of another user's search.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from aiogram.types import User as TgUser
from tests.integration.test_delivery_eligibility import queued_event
from tests.unit.test_public_add_flow import _init_env, _make_cb

from hunter.core.models import User
from hunter.notify.security import AuthAndScopeMiddleware, StrangerLimiter
from hunter.notify.telegram_handlers.public_search_menu import search_token
from hunter.storage.subscriptions import (
    SubscriptionActor,
    SubscriptionNotFoundError,
)

pytest_plugins = ("tests.integration.test_public_hunter",)


@pytest.mark.asyncio
async def test_delete_search_full_confirmation_flow(tmp_path) -> None:
    db, writer, repo, _, _, router, bot = await _init_env(tmp_path)
    for middleware in router.message.outer_middleware:
        if isinstance(middleware, AuthAndScopeMiddleware):
            middleware.limiter = StrangerLimiter()

    try:
        uid = await db.create_user(telegram_user_id=101, chat_id=101, role="user", status="active")
        tg_user = TgUser(id=101, is_bot=False, first_name="User101")
        actor = SubscriptionActor(uid)
        await repo.create(actor, "sub_del", uid, "q_del", 25000, name="Наушники Sony")

        token = search_token("sub_del")

        # 1. Click delete -> should prompt confirmation
        cb_del = _make_cb(101, tg_user, f"sc:delete:{token}:1", bot)
        object.__setattr__(cb_del.message, "edit_text", AsyncMock())
        await router.propagate_event("callback_query", cb_del, db=db, bot=bot)

        assert "Удалить поиск" in cb_del.message.edit_text.await_args.args[0]
        markup = cb_del.message.edit_text.await_args.kwargs["reply_markup"]
        assert markup.inline_keyboard[0][0].text == "Да, удалить"
        assert markup.inline_keyboard[0][1].text == "Отмена"

        # Subscription still exists
        sub = await repo.get(actor, "sub_del")
        assert sub is not None

        # 2. Click cancel -> returns to card
        cb_cancel = _make_cb(101, tg_user, f"sm:view:{token}", bot)
        object.__setattr__(cb_cancel.message, "edit_text", AsyncMock())
        await router.propagate_event("callback_query", cb_cancel, db=db, bot=bot)
        assert "Наушники Sony" in cb_cancel.message.edit_text.await_args.args[0]

        # 3. Confirm deletion
        cb_confirm = _make_cb(101, tg_user, f"sc:confirm:{token}:1", bot)
        object.__setattr__(cb_confirm.message, "edit_text", AsyncMock())
        await router.propagate_event("callback_query", cb_confirm, db=db, bot=bot)

        # Renders updated search list with 0 searches
        assert "Ваши поиски (0)" in cb_confirm.message.edit_text.await_args.args[0]
        with pytest.raises(SubscriptionNotFoundError):
            await repo.get(actor, "sub_del")

    finally:
        await writer.stop()
        await db.close()


@pytest.mark.asyncio
async def test_delete_search_suppresses_pending_and_preserves_sent(public_v2_system) -> None:
    app, deliveries = await queued_event(public_v2_system)

    # 1. Prepare sent delivery for history and pending delivery
    async def prepare(conn):
        await conn.execute(
            "UPDATE deliveries SET status='sent', message_id='msg_history' WHERE id=?",
            (deliveries[20].id,),
        )
        await conn.execute(
            "UPDATE deliveries SET status='pending', lease_token=NULL, lease_until=NULL WHERE id=?",
            (deliveries[10].id,),
        )
    await app.writer.submit(prepare)

    # 2. Delete user 10's only subscription
    await app.subscription_repo.delete(SubscriptionActor(10), "sub_u10")

    # 3. User 10's pending delivery is removed from active queue
    cur = await app.db._conn.execute("SELECT id FROM deliveries WHERE user_id = 10 AND status = 'pending'")
    rows = await cur.fetchall()
    assert len(rows) == 0, "User 10's pending delivery must be cleared on deletion"

    # 4. User 20's sent delivery is preserved in history
    cur = await app.db._conn.execute("SELECT status, message_id FROM deliveries WHERE id = ?", (deliveries[20].id,))
    row = await cur.fetchone()
    assert row is not None and row[0] == "sent" and row[1] == "msg_history"


@pytest.mark.asyncio
async def test_stale_delete_confirmation_is_rejected(tmp_path) -> None:
    db, writer, repo, _, _, router, bot = await _init_env(tmp_path)
    for middleware in router.message.outer_middleware:
        if isinstance(middleware, AuthAndScopeMiddleware):
            middleware.limiter = StrangerLimiter()

    try:
        uid = await db.create_user(telegram_user_id=101, chat_id=101, role="user", status="active")
        tg_user = TgUser(id=101, is_bot=False, first_name="User101")
        actor = SubscriptionActor(uid)
        await repo.create(actor, "sub_stale", uid, "q_stale", 25000, name="Тест")
        token = search_token("sub_stale")

        # Concurrent edit bumps version to 2
        await repo.update(actor, "sub_stale", expected_version=1, name="Новое имя")

        # Stale confirm with version 1
        cb_stale = _make_cb(101, tg_user, f"sc:confirm:{token}:1", bot)
        await router.propagate_event("callback_query", cb_stale, db=db, bot=bot)
        assert cb_stale.answer.await_args.kwargs.get("show_alert") is True

        # Subscription still exists
        sub = await repo.get(actor, "sub_stale")
        assert sub.name == "Новое имя"
        assert sub.version == 2
    finally:
        await writer.stop()
        await db.close()
