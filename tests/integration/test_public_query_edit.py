import sqlite3

import pytest
from tests.integration.test_public_hunter import public_v2_system as public_v2_system

from hunter.application.public_query_edit import finish_query_edit
from hunter.storage.public_edit_dialog import begin_edit
from hunter.storage.subscriptions import SubscriptionActor


@pytest.mark.asyncio
async def test_price_change_and_dialog_finish_are_atomic(public_v2_system):
    app, _, old, _ = public_v2_system
    await begin_edit(app.subscription_repo, SubscriptionActor(10), 100, "sub_u10", 1, "price")
    sub, refreshed = await finish_query_edit(
        app.subscription_service, SubscriptionActor(10), 100, "120000"
    )
    assert refreshed and sub.max_price == 120000 and sub.query_id != old
    assert (await app.subscription_repo.get(SubscriptionActor(20), "sub_u20")).query_id == old
    assert await app.db.get_pending_product(100) is None


@pytest.mark.asyncio
async def test_finish_failure_rolls_back_rebind_and_keeps_dialog(public_v2_system):
    app, _, old, _ = public_v2_system
    await begin_edit(app.subscription_repo, SubscriptionActor(10), 100, "sub_u10", 1, "url")

    async def trigger(conn):
        await conn.execute(
            "CREATE TRIGGER reject_dialog_delete BEFORE DELETE ON pending_products "
            "BEGIN SELECT RAISE(ABORT, 'disk fault'); END"
        )

    await app.writer.submit(trigger)
    with pytest.raises(sqlite3.IntegrityError, match="disk fault"):
        await finish_query_edit(
            app.subscription_service, SubscriptionActor(10), 100, "https://avito.ru/moskva?q=mac"
        )
    sub = await app.subscription_repo.get(SubscriptionActor(10), "sub_u10")
    assert sub.query_id == old and sub.version == 1
    assert await app.db.get_pending_product(100) is not None


@pytest.mark.asyncio
async def test_stale_price_edit_does_not_remove_newer_value(public_v2_system):
    from hunter.storage.subscriptions import StaleSubscriptionVersionError

    app, _, _, _ = public_v2_system
    actor = SubscriptionActor(10)
    await begin_edit(app.subscription_repo, actor, 100, "sub_u10", 1, "price")
    await app.subscription_repo.update(actor, "sub_u10", expected_version=1, name="newer")
    with pytest.raises(StaleSubscriptionVersionError):
        await finish_query_edit(app.subscription_service, actor, 100, "120000")
    assert (await app.subscription_repo.get(actor, "sub_u10")).name == "newer"
    assert await app.db.get_pending_product(100)


@pytest.mark.asyncio
async def test_foreign_actor_cannot_finish_dialog(public_v2_system):
    from hunter.storage.subscriptions import AccessDeniedError

    app, _, _, _ = public_v2_system
    await begin_edit(app.subscription_repo, SubscriptionActor(10), 100, "sub_u10", 1, "url")
    with pytest.raises(AccessDeniedError):
        await finish_query_edit(
            app.subscription_service,
            SubscriptionActor(20, is_owner=True),
            100,
            "https://avito.ru/moskva?q=mac",
        )
    assert await app.db.get_pending_product(100)


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", ["name", "price"])
async def test_replaced_dialog_fences_prepared_query_change(
    public_v2_system, monkeypatch, replacement
):
    from hunter.application import public_query_edit
    from hunter.storage.subscriptions import AccessDeniedError

    app, _, old, _ = public_v2_system
    actor = SubscriptionActor(10)
    await begin_edit(app.subscription_repo, actor, 100, "sub_u10", 1, "price")
    prepare = public_query_edit._prepare

    async def replace_after_prepare(*args):
        result = await prepare(*args)
        await begin_edit(app.subscription_repo, actor, 100, "sub_u10", 1, replacement)
        return result

    monkeypatch.setattr(public_query_edit, "_prepare", replace_after_prepare)
    with pytest.raises(AccessDeniedError, match="Dialog changed"):
        await finish_query_edit(app.subscription_service, actor, 100, "120000")
    assert (await app.subscription_repo.get(actor, "sub_u10")).query_id == old
    assert (await app.db.get_pending_product(100))["payload"]["field"] == replacement


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["name", "price"])
async def test_handler_snapshot_cannot_finish_reopened_dialog(public_v2_system, field):
    from hunter.storage.public_edit_dialog import finish_edit
    from hunter.storage.subscriptions import AccessDeniedError

    app, _, _, _ = public_v2_system
    actor = SubscriptionActor(10)
    await begin_edit(app.subscription_repo, actor, 100, "sub_u10", 1, field)
    old = (await app.db.get_pending_product(100))["payload"]
    await begin_edit(app.subscription_repo, actor, 100, "sub_u10", 1, field)
    operation = finish_query_edit if field == "price" else finish_edit
    target = app.subscription_service if field == "price" else app.subscription_repo
    with pytest.raises(AccessDeniedError, match="Dialog changed"):
        await operation(target, actor, 100, "120000", expected_payload=old)
    assert (await app.subscription_repo.get(actor, "sub_u10")).version == 1
    assert await app.db.get_pending_product(100)
