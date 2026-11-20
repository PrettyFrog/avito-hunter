"""Integration tests for T-B087: URL edit with quiet baseline seeding on query rebind (RB-1).

Verifies that editing search URL:
1. Rebinds subscription to a new query with quiet baseline (0 deliveries on first poll).
2. Deactivates old slot assignment only if no other tenants share the old query.
3. Validates Avito URLs and prompts retry on malformed / external links.
4. Enforces tenant ownership and isolates dialogs.
5. Rolls back atomically on database write failure.
"""

from __future__ import annotations

import sqlite3
from unittest.mock import AsyncMock

import pytest
from aiogram.types import User as TgUser
from tests.unit.test_public_add_flow import _make_msg

from hunter.core.models import ObservedListing, PriceState, User
from hunter.domain.observations import ObservationBatch, ParseOutcome
from hunter.notify.telegram_handlers.public_search_edit import (
    begin_edit,
    handle_public_add_message,
)
from hunter.storage.observation_tx import execute_observation_tx
from hunter.storage.subscriptions import SubscriptionActor

pytest_plugins = ("tests.integration.test_public_hunter",)


@pytest.mark.asyncio
async def test_url_edit_rebinds_to_new_query_with_quiet_baseline(public_v2_system) -> None:
    app, _, old_qid, _ = public_v2_system
    actor = SubscriptionActor(10)
    user = User(id=10, telegram_user_id=10, chat_id=100, role="user", created_at="")

    # 1. Start editing URL
    await begin_edit(app.subscription_repo, actor, 100, "sub_u10", 1, "url")
    assert await app.db.get_pending_product(100) is not None

    # 2. Submit new valid Avito search URL
    new_url = "https://www.avito.ru/moskva/noutbuki/apple-ASgCAgICAUCo6B0U~t43?s=104"
    msg = _make_msg(10, TgUser(id=10, is_bot=False, first_name="User10"), new_url, None)
    object.__setattr__(msg, "chat", type("Chat", (), {"id": 100})())
    object.__setattr__(msg, "reply", AsyncMock())

    handled = await handle_public_add_message(msg, app.db, user, app.subscription_service)
    assert handled is True
    assert "✅ Поиск обновлён" in msg.reply.await_args.args[0]

    # 3. Verify subscription rebound
    sub = await app.subscription_repo.get(actor, "sub_u10")
    new_qid = sub.query_id
    assert new_qid != old_qid
    assert sub.version == 2

    # 4. Old query kept because user 20 (sub_u20) still uses old_qid
    other = await app.subscription_repo.get(SubscriptionActor(20), "sub_u20")
    assert other.query_id == old_qid

    # 5. First poll of new query produces quiet baseline (0 deliveries)
    initial_batch = ObservationBatch(
        slot_id="slot_test",
        query_id=new_qid,
        observed_at="2026-10-30T12:00:00+00:00",
        outcome=ParseOutcome.OK,
        listings=(
            ObservedListing("lot_1", "MacBook Air M1", 50000, PriceState.ACTIVE, "https://avito.ru/1"),
            ObservedListing("lot_2", "MacBook Air M2", 70000, PriceState.ACTIVE, "https://avito.ru/2"),
        ),
    )

    async def ingest_bootstrap(conn):
        return await execute_observation_tx(conn, initial_batch, request_sequence=1)

    res_bootstrap = await app.writer.submit(ingest_bootstrap)
    assert res_bootstrap.is_bootstrap is True
    assert res_bootstrap.deliveries_created == 0, "Quiet baseline must create 0 deliveries"

    # 6. Subsequent poll with a newly published lot creates a delivery
    new_batch = ObservationBatch(
        slot_id="slot_test",
        query_id=new_qid,
        observed_at="2026-10-30T12:05:00+00:00",
        outcome=ParseOutcome.OK,
        listings=(
            ObservedListing("lot_1", "MacBook Air M1", 50000, PriceState.ACTIVE, "https://avito.ru/1"),
            ObservedListing("lot_2", "MacBook Air M2", 70000, PriceState.ACTIVE, "https://avito.ru/2"),
            ObservedListing("lot_3_fresh", "MacBook Pro M1", 60000, PriceState.ACTIVE, "https://avito.ru/3"),
        ),
    )

    async def ingest_fresh(conn):
        return await execute_observation_tx(conn, new_batch, request_sequence=2)

    res_fresh = await app.writer.submit(ingest_fresh)
    assert res_fresh.is_bootstrap is False
    assert res_fresh.deliveries_created == 1, "Fresh lot must produce exactly 1 delivery"


@pytest.mark.asyncio
async def test_url_edit_invalid_input_prompts_retry(public_v2_system) -> None:
    app, _, old_qid, _ = public_v2_system
    actor = SubscriptionActor(10)
    user = User(id=10, telegram_user_id=10, chat_id=100, role="user", created_at="")

    await begin_edit(app.subscription_repo, actor, 100, "sub_u10", 1, "url")

    # Invalid URL (external domain)
    msg = _make_msg(10, TgUser(id=10, is_bot=False, first_name="User10"), "https://google.com/search?q=mac", None)
    object.__setattr__(msg, "chat", type("Chat", (), {"id": 100})())
    object.__setattr__(msg, "reply", AsyncMock())

    handled = await handle_public_add_message(msg, app.db, user, app.subscription_service)
    assert handled is True
    assert "Некорректное значение" in msg.reply.await_args.args[0]

    # Subscription untouched, dialog remains open
    sub = await app.subscription_repo.get(actor, "sub_u10")
    assert sub.query_id == old_qid
    assert sub.version == 1
    assert await app.db.get_pending_product(100) is not None


@pytest.mark.asyncio
async def test_url_edit_foreign_user_cannot_access(public_v2_system) -> None:
    app, _, old_qid, _ = public_v2_system
    actor_owner = SubscriptionActor(10)
    user_stranger = User(id=20, telegram_user_id=20, chat_id=200, role="user", created_at="")

    await begin_edit(app.subscription_repo, actor_owner, 100, "sub_u10", 1, "url")

    msg = _make_msg(20, TgUser(id=20, is_bot=False, first_name="User20"), "https://avito.ru/test", None)
    object.__setattr__(msg, "chat", type("Chat", (), {"id": 100})())
    object.__setattr__(msg, "reply", AsyncMock())

    handled = await handle_public_add_message(msg, app.db, user_stranger, app.subscription_service)
    assert handled is False

    sub = await app.subscription_repo.get(actor_owner, "sub_u10")
    assert sub.query_id == old_qid
    assert sub.version == 1


@pytest.mark.asyncio
async def test_url_edit_db_fault_rolls_back_atomically(public_v2_system) -> None:
    app, _, old_qid, _ = public_v2_system
    actor = SubscriptionActor(10)
    user = User(id=10, telegram_user_id=10, chat_id=100, role="user", created_at="")

    await begin_edit(app.subscription_repo, actor, 100, "sub_u10", 1, "url")

    async def fault(conn):
        await conn.execute(
            "CREATE TRIGGER fail_rebind_url BEFORE UPDATE OF query_id ON subscriptions "
            "BEGIN SELECT RAISE(ABORT, 'injected url write fault'); END"
        )
    await app.writer.submit(fault)

    new_url = "https://www.avito.ru/moskva/noutbuki?s=104"
    msg = _make_msg(10, TgUser(id=10, is_bot=False, first_name="User10"), new_url, None)
    object.__setattr__(msg, "chat", type("Chat", (), {"id": 100})())
    object.__setattr__(msg, "reply", AsyncMock())

    with pytest.raises(sqlite3.IntegrityError, match="injected url write fault"):
        await handle_public_add_message(msg, app.db, user, app.subscription_service)

    sub = await app.subscription_repo.get(actor, "sub_u10")
    assert sub.query_id == old_qid
    assert sub.version == 1
