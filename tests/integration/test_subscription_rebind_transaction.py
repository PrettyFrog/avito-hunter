"""Transactional subscription rebind must isolate tenants and survive faults."""
import asyncio
import sqlite3

import pytest

from hunter.application.subscriptions import compute_query_id
from hunter.storage.subscription_rebind import rebind_subscription
from hunter.storage.subscriptions import SubscriptionActor

pytest_plugins = ("tests.integration.test_public_hunter",)


@pytest.mark.asyncio
async def test_rebind_preserves_other_tenant_and_rejects_old_generation(public_v2_system):
    app, _, old_qid, generation = public_v2_system
    await app.dispatcher.stop()
    repo = app.subscription_service.repo
    url = "https://www.avito.ru/moskva/noutbuki?pmax=120000&s=104"
    new_qid = compute_query_id(url)
    old, new = await rebind_subscription(repo, SubscriptionActor(10), "sub_u10", 1,
                                          new_qid, url, 120000, True, 4)
    assert old == old_qid and new == new_qid
    first = await repo.get(SubscriptionActor(10), "sub_u10")
    second = await repo.get(SubscriptionActor(20), "sub_u20")
    assert (first.query_id, first.max_price) == (new_qid, 120000)
    assert (second.query_id, second.max_price) == (old_qid, 100000)
    assignments = await (await app.db._conn.execute(
        "SELECT query_id FROM assignments WHERE active = 1"
    )).fetchall()
    assert {row[0] for row in assignments} == {old_qid, new_qid}
    assert generation > 0


@pytest.mark.asyncio
async def test_rebind_failure_rolls_back_all_state(public_v2_system):
    app, _, old_qid, _ = public_v2_system
    await app.dispatcher.stop()
    async def fault(c):
        await c.execute("CREATE TRIGGER fail_rebind BEFORE UPDATE OF query_id ON subscriptions "
                        "BEGIN SELECT RAISE(ABORT, 'injected rebind fault'); END")
    await app.writer.submit(fault)
    repo = app.subscription_service.repo
    url = "https://avito.ru/moskva?pmax=120000"
    qid = compute_query_id(url)
    with pytest.raises(sqlite3.IntegrityError, match="injected rebind fault"):
        await rebind_subscription(
            repo, SubscriptionActor(10), "sub_u10", 1, qid, url, 120000, True, 4
        )
    assert (await repo.get(SubscriptionActor(10), "sub_u10")).query_id == old_qid
    row = await (
        await app.db._conn.execute("SELECT COUNT(*) FROM queries WHERE id = ?", (qid,))
    ).fetchone()
    assert row[0] == 0


@pytest.mark.asyncio
async def test_no_spare_capacity_rolls_back_query_and_subscription(public_v2_system):
    from hunter.storage.subscriptions import NoActiveSlotCapacityError
    app, _, old_qid, _ = public_v2_system
    await app.dispatcher.stop()
    async def fill(conn):
        for position in range(1, 4):
            qid = f"q_fill_{position}"
            await conn.execute("INSERT INTO queries(id, canonical_url) VALUES (?, ?)",
                               (qid, f"https://avito.ru/fill{position}"))
            await conn.execute("INSERT INTO assignments(id,slot_id,query_id,position,generation) "
                               "VALUES (?, 'slot_1', ?, ?, ?)",
                               (f"fill_{position}", qid, position, 100 + position))
    await app.writer.submit(fill)
    url = "https://avito.ru/moskva?pmax=120000"
    qid = compute_query_id(url)
    with pytest.raises(NoActiveSlotCapacityError):
        await rebind_subscription(app.subscription_service.repo, SubscriptionActor(10),
                                  "sub_u10", 1, qid, url, 120000, True, 4)
    saved = await app.subscription_service.repo.get(SubscriptionActor(10), "sub_u10")
    assert (saved.query_id, saved.max_price, saved.version) == (old_qid, 100000, 1)
    row = await (
        await app.db._conn.execute("SELECT COUNT(*) FROM queries WHERE id = ?", (qid,))
    ).fetchone()
    assert row[0] == 0


@pytest.mark.asyncio
async def test_existing_target_is_seeded_and_other_tenant_is_isolated(public_v2_system):
    from hunter.storage.subscriptions import AccessDeniedError
    app, _, old_qid, _ = public_v2_system
    await app.dispatcher.stop()
    url = "https://avito.ru/moskva?pmax=120000"
    qid = compute_query_id(url)
    async def seed(conn):
        await conn.execute("INSERT INTO queries(id, canonical_url) VALUES (?, ?)", (qid, url))
        await conn.execute("INSERT INTO listing_state(avito_id, revision) VALUES ('known', 7)")
        await conn.execute("INSERT INTO query_membership(query_id,avito_id,first_seen,last_seen) "
                           "VALUES (?, 'known', 't', 't')", (qid,))
    await app.writer.submit(seed)
    repo = app.subscription_service.repo
    with pytest.raises(AccessDeniedError):
        await rebind_subscription(
            repo, SubscriptionActor(20), "sub_u10", 1, qid, url, 120000, True, 4
        )
    assert (await repo.get(SubscriptionActor(10), "sub_u10")).query_id == old_qid
    await rebind_subscription(repo, SubscriptionActor(10), "sub_u10", 1, qid, url, 120000, True, 4)
    seen = await (await app.db._conn.execute(
        "SELECT baseline_epoch, last_revision FROM subscription_seen "
        "WHERE subscription_id = 'sub_u10' AND avito_id = 'known'"
    )).fetchone()
    assert tuple(seen) == (1, 7)


@pytest.mark.asyncio
async def test_competing_rebinds_fence_stale_version(public_v2_system):
    from hunter.storage.subscriptions import StaleSubscriptionVersionError
    app, _, _, _ = public_v2_system
    await app.dispatcher.stop()
    repo = app.subscription_service.repo
    actor = SubscriptionActor(10)
    urls = [f"https://avito.ru/moskva?pmax={price}" for price in (120000, 130000)]
    results = await asyncio.gather(*(
        rebind_subscription(repo, actor, "sub_u10", 1, compute_query_id(url), url, price, True, 4)
        for url, price in zip(urls, (120000, 130000), strict=True)
    ), return_exceptions=True)
    assert sum(isinstance(result, StaleSubscriptionVersionError) for result in results) == 1
    changed = await repo.get(actor, "sub_u10")
    assert changed.version == 2 and changed.max_price in (120000, 130000)
