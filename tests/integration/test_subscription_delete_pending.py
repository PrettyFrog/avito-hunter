import pytest
from tests.integration.test_delivery_eligibility import queued_event

from hunter.storage.subscriptions import TOMB_SQL, SubscriptionActor

pytest_plugins = ("tests.integration.test_public_hunter",)


@pytest.mark.asyncio
async def test_deleting_one_search_preserves_other_eligible_search_intent(public_v2_system):
    app, deliveries = await queued_event(public_v2_system)
    other = await app.subscription_service.add_subscription(
        10, "https://avito.ru/moskva?q=mac", 120000, sub_id="sub_other"
    )

    async def prepare(conn):
        await conn.execute(
            "UPDATE deliveries SET status='pending', lease_token=NULL, lease_until=NULL WHERE id=?",
            (deliveries[10].id,),
        )
        await conn.execute(
            "UPDATE queries SET bootstrap_state='ready' WHERE id=?", (other.query_id,)
        )
        await conn.execute(
            "INSERT INTO query_membership(query_id,avito_id,first_seen,last_seen) "
            "VALUES (?, 'lot1', '2026-10-28T21:00:00+00:00', '2026-10-28T21:00:00+00:00')",
            (other.query_id,),
        )

    await app.writer.submit(prepare)
    await app.subscription_repo.delete(SubscriptionActor(10), "sub_u10")
    cursor = await app.db._conn.execute(
        "SELECT status FROM deliveries WHERE id=?", (deliveries[10].id,)
    )
    row = await cursor.fetchone()
    assert row is not None and row[0] == "pending"
    await app.dispatcher.dispatch_once("2026-10-28T21:00:30+00:00")
    assert [uid for uid, _ in public_v2_system[1].sent] == [10]


@pytest.mark.asyncio
async def test_deleting_last_search_keeps_other_users_intents(public_v2_system):
    app, deliveries = await queued_event(public_v2_system)

    async def prepare(conn):
        await conn.execute(
            "UPDATE deliveries SET status='pending', lease_token=NULL, lease_until=NULL"
        )

    await app.writer.submit(prepare)
    await app.subscription_repo.delete(SubscriptionActor(10), "sub_u10")
    cursor = await app.db._conn.execute("SELECT user_id FROM deliveries ORDER BY user_id")
    assert [row[0] for row in await cursor.fetchall()] == [20]


@pytest.mark.asyncio
async def test_delete_write_failure_rolls_back_subscription_and_delivery(public_v2_system):
    import sqlite3

    app, deliveries = await queued_event(public_v2_system)

    async def prepare(conn):
        await conn.execute(
            "UPDATE deliveries SET status='pending', lease_token=NULL, lease_until=NULL WHERE id=?",
            (deliveries[10].id,),
        )
        await conn.execute(TOMB_SQL)
        await conn.execute(
            "CREATE TRIGGER reject_tombstone BEFORE INSERT ON subscription_tombstones "
            "BEGIN SELECT RAISE(ABORT, 'disk fault'); END"
        )

    await app.writer.submit(prepare)
    with pytest.raises(sqlite3.IntegrityError, match="disk fault"):
        await app.subscription_repo.delete(SubscriptionActor(10), "sub_u10")
    assert (await app.subscription_repo.get(SubscriptionActor(10), "sub_u10")).id == "sub_u10"
    cursor = await app.db._conn.execute(
        "SELECT status FROM deliveries WHERE id=?", (deliveries[10].id,)
    )
    assert (await cursor.fetchone())[0] == "pending"


@pytest.mark.asyncio
async def test_unrelated_remaining_search_suppresses_old_intent(public_v2_system):
    app, deliveries = await queued_event(public_v2_system)
    await app.subscription_service.add_subscription(
        10, "https://avito.ru/moskva?q=mac", 80000, sub_id="sub_other"
    )

    async def prepare(conn):
        await conn.execute(
            "UPDATE deliveries SET status='pending', lease_token=NULL, lease_until=NULL WHERE id=?",
            (deliveries[10].id,),
        )

    await app.writer.submit(prepare)
    await app.subscription_repo.delete(SubscriptionActor(10), "sub_u10")
    await app.dispatcher.dispatch_once("2026-10-28T21:00:30+00:00")
    cursor = await app.db._conn.execute(
        "SELECT status FROM deliveries WHERE id=?", (deliveries[10].id,)
    )
    assert (await cursor.fetchone())[0] == "suppressed"
    assert not [uid for uid, _ in public_v2_system[1].sent if uid == 10]
