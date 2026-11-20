"""Tenant and version guards for menu pause/resume operations."""
import sqlite3

import pytest
from tests.integration.test_public_hunter import public_v2_system as public_v2_system

from hunter.storage.subscription_controls import set_subscription_enabled
from hunter.storage.subscriptions import (
    AccessDeniedError,
    StaleSubscriptionVersionError,
    SubscriptionActor,
)


@pytest.mark.asyncio
async def test_toggle_is_scoped_versioned_and_silent_on_resume(public_v2_system):
    app, _, qid, _ = public_v2_system
    await app.dispatcher.stop()
    actor = SubscriptionActor(10)
    await set_subscription_enabled(app.subscription_repo, actor, "sub_u10", 1, False)
    assert not (await app.subscription_repo.get(actor, "sub_u10")).enabled
    assert (await app.subscription_repo.get(SubscriptionActor(20), "sub_u20")).enabled
    with pytest.raises(StaleSubscriptionVersionError):
        await set_subscription_enabled(app.subscription_repo, actor, "sub_u10", 1, True)
    with pytest.raises(AccessDeniedError):
        await set_subscription_enabled(
            app.subscription_repo, SubscriptionActor(20, is_owner=True), "sub_u10", 2, True
        )

    async def seed(conn):
        await conn.execute("INSERT INTO listing_state(avito_id,revision) VALUES ('paused',3)")
        await conn.execute(
            "INSERT INTO query_membership(query_id,avito_id,first_seen,last_seen) "
            "VALUES (?,'paused','t','t')", (qid,)
        )

    await app.writer.submit(seed)
    rec = await set_subscription_enabled(app.subscription_repo, actor, "sub_u10", 2, True)
    assert rec.enabled and rec.version == 3
    row = await (await app.db._conn.execute(
        "SELECT last_revision FROM subscription_seen "
        "WHERE subscription_id='sub_u10' AND avito_id='paused'"
    )).fetchone()
    assert row[0] == 3


@pytest.mark.asyncio
async def test_resume_fault_keeps_pause_and_baseline_unchanged(public_v2_system):
    app, _, qid, _ = public_v2_system
    await app.dispatcher.stop()
    actor = SubscriptionActor(10)
    await set_subscription_enabled(app.subscription_repo, actor, "sub_u10", 1, False)

    async def fault(conn):
        await conn.execute("INSERT INTO listing_state(avito_id,revision) VALUES ('fault-paused',1)")
        await conn.execute(
            "INSERT INTO query_membership(query_id,avito_id,first_seen,last_seen) "
            "VALUES (?,'fault-paused','t','t')", (qid,)
        )
        await conn.execute(
            "CREATE TRIGGER reject_resume BEFORE UPDATE OF enabled ON subscriptions "
            "BEGIN SELECT RAISE(ABORT, 'resume fault'); END"
        )

    await app.writer.submit(fault)
    with pytest.raises(sqlite3.IntegrityError, match="resume fault"):
        await set_subscription_enabled(app.subscription_repo, actor, "sub_u10", 2, True)
    rec = await app.subscription_repo.get(actor, "sub_u10")
    assert not rec.enabled and rec.version == 2
    row = await (await app.db._conn.execute(
        "SELECT count(*) FROM subscription_seen "
        "WHERE subscription_id='sub_u10' AND avito_id='fault-paused'"
    )).fetchone()
    assert row[0] == 0


@pytest.mark.asyncio
async def test_pause_frees_slot_assignment_and_allows_slot_reuse(public_v2_system):
    app, _, qid, _ = public_v2_system
    await app.dispatcher.stop()
    repo = app.subscription_repo
    app.subscription_service.max_queries_per_slot = 1

    # Both users 10 and 20 are subscribed to qid in public_v2_system.
    # User 10 pauses sub_u10. User 20 is still enabled.
    await set_subscription_enabled(repo, SubscriptionActor(10), "sub_u10", 1, False)
    # Since user 20 is still active on qid, assignment must remain active = 1
    row = await (await app.db._conn.execute(
        "SELECT count(*) FROM assignments WHERE query_id=? AND active=1", (qid,)
    )).fetchone()
    assert row[0] == 1

    # Now user 20 pauses sub_u20. No more enabled subscriptions for qid!
    await set_subscription_enabled(repo, SubscriptionActor(20), "sub_u20", 1, False)
    # Assignment MUST become active = 0 (freed from slot)!
    row = await (await app.db._conn.execute(
        "SELECT count(*) FROM assignments WHERE query_id=? AND active=1", (qid,)
    )).fetchone()
    assert row[0] == 0

    # With capacity = 1, we can now add a brand new search that takes the freed slot!
    replacement = await app.subscription_service.add_subscription(
        10, "https://avito.ru/moskva?q=vacated_slot", 50000
    )
    assert replacement.query_id != qid
    row = await (await app.db._conn.execute(
        "SELECT count(*) FROM assignments WHERE query_id=? AND active=1", (replacement.query_id,)
    )).fetchone()
    assert row[0] == 1


@pytest.mark.asyncio
async def test_resume_reclaims_slot_and_rejects_when_full(public_v2_system):
    from hunter.storage.subscriptions import NoActiveSlotCapacityError

    app, _, qid, _ = public_v2_system
    await app.dispatcher.stop()
    repo = app.subscription_repo
    app.subscription_service.max_queries_per_slot = 1

    # Pause both subscriptions so slot is freed
    await set_subscription_enabled(repo, SubscriptionActor(10), "sub_u10", 1, False)
    await set_subscription_enabled(repo, SubscriptionActor(20), "sub_u20", 1, False)
    row = await (await app.db._conn.execute(
        "SELECT count(*) FROM assignments WHERE query_id=? AND active=1", (qid,)
    )).fetchone()
    assert row[0] == 0

    # New search takes the only slot position
    other = await app.subscription_service.add_subscription(
        10, "https://avito.ru/moskva?q=occupier", 50000
    )

    # Attempting to resume sub_u10 must raise NoActiveSlotCapacityError
    with pytest.raises(NoActiveSlotCapacityError):
        await set_subscription_enabled(repo, SubscriptionActor(10), "sub_u10", 2, True, max_queries_per_slot=1)

    # State remains paused
    rec = await repo.get(SubscriptionActor(10), "sub_u10")
    assert not rec.enabled and rec.version == 2

    # Pause the occupier so slot is freed again
    await set_subscription_enabled(repo, SubscriptionActor(10), other.id, other.version, False)

    # Now resume sub_u10 succeeds!
    rec = await set_subscription_enabled(repo, SubscriptionActor(10), "sub_u10", 2, True, max_queries_per_slot=1)
    assert rec.enabled and rec.version == 3
    row = await (await app.db._conn.execute(
        "SELECT count(*) FROM assignments WHERE query_id=? AND active=1", (qid,)
    )).fetchone()
    assert row[0] == 1


@pytest.mark.asyncio
async def test_delete_frees_slot_when_remaining_subscription_is_paused(public_v2_system):
    app, _, qid, _ = public_v2_system
    await app.dispatcher.stop()
    repo = app.subscription_repo
    app.subscription_service.max_queries_per_slot = 1

    # User 10 pauses sub_u10
    await set_subscription_enabled(repo, SubscriptionActor(10), "sub_u10", 1, False)

    # User 20 deletes their subscription sub_u20.
    # The remaining subscription sub_u10 is PAUSED (enabled = 0).
    # Therefore, no enabled subscriptions remain, and assignment MUST become active = 0!
    await repo.delete(SubscriptionActor(20), "sub_u20")
    row = await (await app.db._conn.execute(
        "SELECT count(*) FROM assignments WHERE query_id=? AND active=1", (qid,)
    )).fetchone()
    assert row[0] == 0


@pytest.mark.asyncio
async def test_assign_query_reconciles_stale_zombie_assignments(public_v2_system):
    app, _, qid, _ = public_v2_system
    await app.dispatcher.stop()
    repo = app.subscription_repo
    conn = app.db._conn

    # Capacity is 2
    app.subscription_service.max_queries_per_slot = 2

    # Pause original subscriptions, but simulate a legacy zombie assignment (active=1, no enabled subs)
    await set_subscription_enabled(repo, SubscriptionActor(10), "sub_u10", 1, False)
    await set_subscription_enabled(repo, SubscriptionActor(20), "sub_u20", 1, False)
    await conn.execute("UPDATE assignments SET active = 1 WHERE query_id = ?", (qid,))
    await conn.commit()

    # With capacity 2, adding 2 new searches must succeed if zombie assignment is auto-reconciled
    s1 = await app.subscription_service.add_subscription(10, "https://avito.ru/moskva?q=s1", 10000)
    s2 = await app.subscription_service.add_subscription(10, "https://avito.ru/moskva?q=s2", 20000)

    row = await (await conn.execute(
        "SELECT count(*) FROM assignments WHERE active = 1 AND query_id IN (?, ?)",
        (s1.query_id, s2.query_id),
    )).fetchone()
    assert row[0] == 2
