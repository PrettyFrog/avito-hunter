"""Deleting a subscription preserves shared work and releases orphan capacity."""
import pytest
from tests.integration.test_public_hunter import public_v2_system as public_v2_system

from hunter.storage.subscriptions import StaleSubscriptionVersionError, SubscriptionActor


@pytest.mark.asyncio
async def test_delete_releases_only_last_subscribers_assignment(public_v2_system):
    app, _, qid, _ = public_v2_system
    await app.dispatcher.stop()
    repo = app.subscription_repo
    await repo.delete(SubscriptionActor(10), "sub_u10")
    row = await (await app.db._conn.execute(
        "SELECT count(*) FROM assignments WHERE query_id=? AND active=1", (qid,)
    )).fetchone()
    assert row[0] == 1
    await repo.delete(SubscriptionActor(20), "sub_u20")
    row = await (await app.db._conn.execute(
        "SELECT count(*) FROM assignments WHERE query_id=? AND active=1", (qid,)
    )).fetchone()
    assert row[0] == 0
    app.subscription_service.max_queries_per_slot = 1
    replacement = await app.subscription_service.add_subscription(
        10, "https://avito.ru/moskva?q=new", 100000
    )
    assert replacement.query_id != qid


@pytest.mark.asyncio
async def test_versioned_delete_preserves_newer_subscription(public_v2_system):
    app, _, _, _ = public_v2_system
    await app.dispatcher.stop()
    repo, actor = app.subscription_repo, SubscriptionActor(10)
    await repo.update(actor, "sub_u10", expected_version=1, name="Новое название")
    with pytest.raises(StaleSubscriptionVersionError):
        await repo.delete(actor, "sub_u10", expected_version=1)
    assert (await repo.get(actor, "sub_u10")).name == "Новое название"


@pytest.mark.asyncio
async def test_assignment_write_fault_rolls_back_last_delete(public_v2_system):
    import sqlite3

    app, _, qid, _ = public_v2_system
    await app.dispatcher.stop()
    repo = app.subscription_repo
    await repo.delete(SubscriptionActor(10), "sub_u10")

    async def fault(conn):
        await conn.execute(
            "CREATE TRIGGER reject_retire BEFORE UPDATE OF active ON assignments "
            "BEGIN SELECT RAISE(ABORT, 'assignment fault'); END"
        )

    await app.writer.submit(fault)
    with pytest.raises(sqlite3.IntegrityError, match="assignment fault"):
        await repo.delete(SubscriptionActor(20), "sub_u20", expected_version=1)
    assert (await repo.get(SubscriptionActor(20), "sub_u20")).query_id == qid
    assert not await repo.is_tombstoned("sub_u20")
