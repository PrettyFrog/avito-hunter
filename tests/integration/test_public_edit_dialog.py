import sqlite3

import pytest
from tests.integration.test_public_hunter import public_v2_system as public_v2_system

from hunter.storage.public_edit_dialog import begin_edit, finish_edit
from hunter.storage.subscriptions import (
    AccessDeniedError,
    StaleSubscriptionVersionError,
    SubscriptionActor,
)


@pytest.mark.asyncio
async def test_name_and_stopword_edits_are_tenant_scoped(public_v2_system):
    app, _, _, _ = public_v2_system
    repo = app.subscription_repo
    await begin_edit(repo, SubscriptionActor(10), 100, "sub_u10", 1, "name")
    await finish_edit(repo, SubscriptionActor(10), 100, "Телефоны")
    assert (await repo.get(SubscriptionActor(10), "sub_u10")).name == "Телефоны"
    await repo.update(SubscriptionActor(10), "sub_u10", expected_version=2, words="битый")
    await begin_edit(repo, SubscriptionActor(10), 100, "sub_u10", 3, "words")
    await finish_edit(repo, SubscriptionActor(10), 100, "-")
    assert (await repo.get(SubscriptionActor(10), "sub_u10")).manual_stopwords == ""
    assert (await repo.get(SubscriptionActor(20), "sub_u20")).manual_stopwords == "битый"
    assert await app.db.get_pending_product(100) is None


@pytest.mark.asyncio
async def test_forged_and_stale_edits_are_rejected(public_v2_system):
    app, _, _, _ = public_v2_system
    repo = app.subscription_repo
    with pytest.raises(AccessDeniedError):
        await begin_edit(repo, SubscriptionActor(20, is_owner=True), 200, "sub_u10", 1, "name")
    await begin_edit(repo, SubscriptionActor(10), 100, "sub_u10", 1, "name")
    await repo.update(SubscriptionActor(10), "sub_u10", expected_version=1, name="Новее")
    with pytest.raises(StaleSubscriptionVersionError):
        await finish_edit(repo, SubscriptionActor(10), 100, "Устарело")
    assert (await repo.get(SubscriptionActor(10), "sub_u10")).name == "Новее"


@pytest.mark.asyncio
async def test_fault_rolls_back_change_and_keeps_dialog(public_v2_system):
    app, _, _, _ = public_v2_system
    repo = app.subscription_repo
    await begin_edit(repo, SubscriptionActor(10), 100, "sub_u10", 1, "name")

    async def trigger(conn):
        await conn.execute(
            "CREATE TRIGGER reject_edit BEFORE DELETE ON pending_products "
            "BEGIN SELECT RAISE(ABORT, 'disk fault'); END"
        )

    await app.writer.submit(trigger)
    with pytest.raises(sqlite3.IntegrityError, match="disk fault"):
        await finish_edit(repo, SubscriptionActor(10), 100, "Имя")
    assert (await repo.get(SubscriptionActor(10), "sub_u10")).name == ""
    assert await app.db.get_pending_product(100) is not None


@pytest.mark.asyncio
async def test_invalid_input_and_foreign_dialog_do_not_change_state(public_v2_system):
    app, _, _, _ = public_v2_system
    repo = app.subscription_repo
    await begin_edit(repo, SubscriptionActor(10), 100, "sub_u10", 1, "name")
    with pytest.raises(AccessDeniedError):
        await finish_edit(repo, SubscriptionActor(20), 100, "Чужое")
    with pytest.raises(ValueError):
        await finish_edit(repo, SubscriptionActor(10), 100, "   ")
    assert (await repo.get(SubscriptionActor(10), "sub_u10")).version == 1
    assert await app.db.get_pending_product(100)


@pytest.mark.asyncio
async def test_concurrent_finishes_only_commit_once(public_v2_system):
    import asyncio

    app, _, _, _ = public_v2_system
    repo = app.subscription_repo
    await begin_edit(repo, SubscriptionActor(10), 100, "sub_u10", 1, "name")
    results = await asyncio.gather(
        finish_edit(repo, SubscriptionActor(10), 100, "Первое"),
        finish_edit(repo, SubscriptionActor(10), 100, "Второе"),
        return_exceptions=True,
    )
    assert sum(r is None for r in results) == 1
    assert (await repo.get(SubscriptionActor(10), "sub_u10")).version == 2
