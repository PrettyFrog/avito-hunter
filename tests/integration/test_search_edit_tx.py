"""Integration tests for T-B086: Transactional max_price edit in search menu (RB-1).

Verifies that editing max_price:
1. Atomically rebinds subscription to new canonical query with pmax ceiling.
2. Validates positive integer rubles and prompts retry on invalid input.
3. Enforces tenant ownership and rejects foreign users.
4. Enforces CAS version consistency (rejects stale edits).
5. Rolls back atomically on database write failure.
"""

from __future__ import annotations

import sqlite3
from unittest.mock import AsyncMock

import pytest
from aiogram.types import User as TgUser
from tests.unit.test_public_add_flow import _make_msg

from hunter.core.models import User
from hunter.notify.telegram_handlers.public_search_edit import (
    begin_edit,
    handle_public_add_message,
)
from hunter.storage.subscriptions import (
    AccessDeniedError,
    StaleSubscriptionVersionError,
    SubscriptionActor,
)

pytest_plugins = ("tests.integration.test_public_hunter",)


@pytest.mark.asyncio
async def test_price_edit_success_updates_canonical_and_increments_version(public_v2_system) -> None:
    app, _, old_qid, _ = public_v2_system
    actor = SubscriptionActor(10)
    user = User(id=10, telegram_user_id=10, chat_id=100, role="user", created_at="")

    # 1. Start editing price
    await begin_edit(app.subscription_repo, actor, 100, "sub_u10", 1, "price")
    assert await app.db.get_pending_product(100) is not None

    # 2. User submits new valid price
    msg = _make_msg(10, TgUser(id=10, is_bot=False, first_name="User10"), " 150 000 руб ", None)
    object.__setattr__(msg, "chat", type("Chat", (), {"id": 100})())
    object.__setattr__(msg, "reply", AsyncMock())

    handled = await handle_public_add_message(msg, app.db, user, app.subscription_service)
    assert handled is True
    assert "✅ Поиск обновлён" in msg.reply.await_args.args[0]

    # 3. Verify subscription updated atomically
    sub = await app.subscription_repo.get(actor, "sub_u10")
    assert sub.max_price == 150000
    assert sub.version == 2
    assert sub.query_id != old_qid

    # 4. Verify canonical url updated
    reader = await app.subscription_repo.mgr.open_read_connection()
    try:
        cur = await reader.execute("SELECT canonical_url FROM queries WHERE id = ?", (sub.query_id,))
        row = await cur.fetchone()
        assert row is not None and "pmax=150000" in row[0]
    finally:
        await app.subscription_repo.mgr.close_reader(reader)

    # 5. Dialog cleared
    assert await app.db.get_pending_product(100) is None


@pytest.mark.asyncio
async def test_price_edit_invalid_input_prompts_retry_without_breaking_dialog(public_v2_system) -> None:
    app, _, old_qid, _ = public_v2_system
    actor = SubscriptionActor(10)
    user = User(id=10, telegram_user_id=10, chat_id=100, role="user", created_at="")

    await begin_edit(app.subscription_repo, actor, 100, "sub_u10", 1, "price")

    # User submits negative / invalid text
    msg = _make_msg(10, TgUser(id=10, is_bot=False, first_name="User10"), "минус 500", None)
    object.__setattr__(msg, "chat", type("Chat", (), {"id": 100})())
    object.__setattr__(msg, "reply", AsyncMock())

    handled = await handle_public_add_message(msg, app.db, user, app.subscription_service)
    assert handled is True
    assert "Некорректное значение" in msg.reply.await_args.args[0]

    # Subscription untouched
    sub = await app.subscription_repo.get(actor, "sub_u10")
    assert sub.max_price == 100000
    assert sub.version == 1
    assert sub.query_id == old_qid

    # Dialog is still active for user to retry
    assert await app.db.get_pending_product(100) is not None


@pytest.mark.asyncio
async def test_price_edit_foreign_user_cannot_access_or_finish(public_v2_system) -> None:
    app, _, old_qid, _ = public_v2_system
    actor_owner = SubscriptionActor(10)
    user_stranger = User(id=20, telegram_user_id=20, chat_id=200, role="user", created_at="")

    # Dialog started by user 10
    await begin_edit(app.subscription_repo, actor_owner, 100, "sub_u10", 1, "price")

    # User 20 tries to submit into user 10's chat
    msg = _make_msg(20, TgUser(id=20, is_bot=False, first_name="User20"), "120000", None)
    object.__setattr__(msg, "chat", type("Chat", (), {"id": 100})())
    object.__setattr__(msg, "reply", AsyncMock())

    handled = await handle_public_add_message(msg, app.db, user_stranger, app.subscription_service)
    assert handled is False  # Rejected by ownership check

    sub = await app.subscription_repo.get(actor_owner, "sub_u10")
    assert sub.max_price == 100000
    assert sub.version == 1


@pytest.mark.asyncio
async def test_price_edit_stale_version_cancels_dialog(public_v2_system) -> None:
    app, _, _, _ = public_v2_system
    actor = SubscriptionActor(10)
    user = User(id=10, telegram_user_id=10, chat_id=100, role="user", created_at="")

    await begin_edit(app.subscription_repo, actor, 100, "sub_u10", 1, "price")

    # Concurrent modification increments version to 2
    await app.subscription_repo.update(actor, "sub_u10", expected_version=1, name="Concurrent Name")

    msg = _make_msg(10, TgUser(id=10, is_bot=False, first_name="User10"), "120000", None)
    object.__setattr__(msg, "chat", type("Chat", (), {"id": 100})())
    object.__setattr__(msg, "reply", AsyncMock())

    handled = await handle_public_add_message(msg, app.db, user, app.subscription_service)
    assert handled is True
    assert "Поиск изменился или недоступен" in msg.reply.await_args.args[0]

    # Stale dialog cancelled
    assert await app.db.get_pending_product(100) is None


@pytest.mark.asyncio
async def test_price_edit_db_fault_rolls_back_atomically(public_v2_system) -> None:
    app, _, old_qid, _ = public_v2_system
    actor = SubscriptionActor(10)
    user = User(id=10, telegram_user_id=10, chat_id=100, role="user", created_at="")

    await begin_edit(app.subscription_repo, actor, 100, "sub_u10", 1, "price")

    # Inject SQLite abort trigger
    async def fault(conn):
        await conn.execute(
            "CREATE TRIGGER fail_rebind_price BEFORE UPDATE OF max_price ON subscriptions "
            "BEGIN SELECT RAISE(ABORT, 'injected price write fault'); END"
        )
    await app.writer.submit(fault)

    msg = _make_msg(10, TgUser(id=10, is_bot=False, first_name="User10"), "120000", None)
    object.__setattr__(msg, "chat", type("Chat", (), {"id": 100})())
    object.__setattr__(msg, "reply", AsyncMock())

    with pytest.raises(sqlite3.IntegrityError, match="injected price write fault"):
        await handle_public_add_message(msg, app.db, user, app.subscription_service)

    # All state preserved in rollback
    sub = await app.subscription_repo.get(actor, "sub_u10")
    assert sub.max_price == 100000
    assert sub.version == 1
    assert sub.query_id == old_qid


@pytest.mark.asyncio
async def test_keywords_edit_and_clear(public_v2_system) -> None:
    app, _, _, _ = public_v2_system
    actor = SubscriptionActor(10)
    user = User(id=10, telegram_user_id=10, chat_id=100, role="user", created_at="")
    await begin_edit(app.subscription_repo, actor, 100, "sub_u10", 1, "keywords")
    msg = _make_msg(10, TgUser(id=10, is_bot=False, first_name="User10"), "crucial,corsair", None)
    object.__setattr__(msg, "chat", type("Chat", (), {"id": 100})())
    object.__setattr__(msg, "reply", AsyncMock())
    assert await handle_public_add_message(msg, app.db, user, app.subscription_service)
    assert (await app.subscription_repo.get(actor, "sub_u10")).manual_keywords == "crucial,corsair"
    await begin_edit(app.subscription_repo, actor, 100, "sub_u10", 2, "keywords")
    msg_clear = _make_msg(10, TgUser(id=10, is_bot=False, first_name="User10"), "-", None)
    object.__setattr__(msg_clear, "chat", type("Chat", (), {"id": 100})())
    object.__setattr__(msg_clear, "reply", AsyncMock())
    assert await handle_public_add_message(msg_clear, app.db, user, app.subscription_service)
    assert (await app.subscription_repo.get(actor, "sub_u10")).manual_keywords == ""
