"""Outbox delivery must obey current subscription filters across channels."""
import sqlite3
from dataclasses import replace

import pytest
from tests.integration.test_delivery_eligibility import queued_event
from tests.integration.test_quiet_digest_runtime import prepare

from hunter.application.delivery_eligibility import suppress_if_still_ineligible
from hunter.application.dispatcher import SendResult
from hunter.application.subscriptions import compute_query_id
from hunter.core.models import User
from hunter.notify.telegram_handlers.public_status import edit_subscription
from hunter.storage.subscription_rebind import rebind_subscription
from hunter.storage.subscriptions import SubscriptionActor

pytest_plugins = ("tests.integration.test_public_hunter",)


def owner():
    return User(id=10, telegram_user_id=10, chat_id=10, role="user", created_at="")


async def release_claimed(app):
    async def release(conn):
        await conn.execute(
            "UPDATE deliveries SET status='pending', available_at='2026-10-28T21:00:00+00:00', "
            "lease_token=NULL, lease_until=NULL WHERE user_id IN (10,20)"
        )
    await app.writer.submit(release)


async def delivery_status(app, delivery):
    cursor = await app.db._conn.execute("SELECT status FROM deliveries WHERE id=?", (delivery.id,))
    return (await cursor.fetchone())[0]


@pytest.mark.asyncio
async def test_lowered_ceiling_suppresses_old_pending_delivery(public_v2_system):
    app, deliveries = await queued_event(public_v2_system)
    assert (await edit_subscription(owner(), "sub_u10", 80000, None, app.db))[0]
    await release_claimed(app)
    await app.dispatcher.dispatch_once("2026-10-28T21:00:30+00:00")
    assert await delivery_status(app, deliveries[10]) == "suppressed"
    assert await delivery_status(app, deliveries[20]) == "sent"
    assert [uid for uid, _ in public_v2_system[1].sent] == [20]


@pytest.mark.asyncio
async def test_unverified_other_search_defers_instead_of_losing_event(public_v2_system):
    app, deliveries = await queued_event(public_v2_system)
    assert (await edit_subscription(owner(), "sub_u10", 80000, None, app.db))[0]
    await app.subscription_service.add_subscription(
        10, "https://avito.ru/moskva?q=mac", 120000, sub_id="sub_other"
    )
    await release_claimed(app)
    await app.dispatcher.dispatch_once("2026-10-28T21:00:30+00:00")
    assert await delivery_status(app, deliveries[10]) == "pending"
    assert await delivery_status(app, deliveries[20]) == "sent"


@pytest.mark.asyncio
async def test_stale_lease_cannot_suppress_delivery(public_v2_system):
    app, deliveries = await queued_event(public_v2_system)
    delivery = deliveries[10]
    assert (await edit_subscription(owner(), "sub_u10", 80000, None, app.db))[0]
    assert not await suppress_if_still_ineligible(
        app.delivery_repo, replace(delivery, lease_token="stale-token")
    )
    cursor = await app.db._conn.execute(
        "SELECT status, lease_token FROM deliveries WHERE id=?", (delivery.id,)
    )
    assert tuple(await cursor.fetchone()) == ("leased", delivery.lease_token)


@pytest.mark.asyncio
async def test_quiet_digest_excludes_obsolete_user_events(public_v2_system):
    app, bot = await prepare(public_v2_system)
    assert (await edit_subscription(owner(), "sub_u10", 60000, None, app.db))[0]
    await app.dispatcher.dispatch_once("2026-10-27T04:00:01+00:00")
    calls = bot.send_message.call_args_list
    assert not [call for call in calls if str(call.args[0]) == "100"]
    assert len([call for call in calls if str(call.args[0]) == "200"]) == 2
    cursor = await app.db._conn.execute("SELECT status FROM deliveries WHERE user_id=10")
    rows = await cursor.fetchall()
    assert len(rows) == 2 and all(row[0] == "suppressed" for row in rows)


@pytest.mark.asyncio
async def test_vk_fallback_obeys_changed_ceiling(public_v2_system):
    app, deliveries = await queued_event(public_v2_system)
    assert (await edit_subscription(owner(), "sub_u10", 80000, None, app.db))[0]
    sent = []
    async def send(delivery):
        sent.append(delivery.user_id)
        return SendResult("sent", message_id="1")
    app.dispatcher.senders["vk"] = send
    async def release(conn):
        await conn.execute(
            "UPDATE deliveries SET channel='vk', status='pending', "
            "available_at='2026-10-28T21:00:00+00:00', lease_token=NULL, lease_until=NULL "
            "WHERE id=?", (deliveries[10].id,),
        )
    await app.writer.submit(release)
    await app.dispatcher.dispatch_once("2026-10-28T21:00:30+00:00")
    assert await delivery_status(app, deliveries[10]) == "suppressed" and not sent


@pytest.mark.asyncio
async def test_changed_stopword_suppresses_only_matching_user(public_v2_system):
    app, deliveries = await queued_event(public_v2_system)
    assert (await edit_subscription(owner(), "sub_u10", None, "macbook", app.db))[0]
    await release_claimed(app)
    await app.dispatcher.dispatch_once("2026-10-28T21:00:30+00:00")
    assert await delivery_status(app, deliveries[10]) == "suppressed"
    assert await delivery_status(app, deliveries[20]) == "sent"


@pytest.mark.asyncio
async def test_changed_url_with_unobserved_query_defers(public_v2_system):
    app, deliveries = await queued_event(public_v2_system)
    url = "https://avito.ru/sankt-peterburg?pmax=120000&s=104"
    await rebind_subscription(
        app.subscription_repo, SubscriptionActor(10), "sub_u10", 1,
        compute_query_id(url), url, 120000, True, 4,
    )
    await release_claimed(app)
    await app.dispatcher.dispatch_once("2026-10-28T21:00:30+00:00")
    assert await delivery_status(app, deliveries[10]) == "pending"


@pytest.mark.asyncio
async def test_suppress_write_fault_leaves_lease_and_does_not_send(public_v2_system):
    app, deliveries = await queued_event(public_v2_system)
    assert (await edit_subscription(owner(), "sub_u10", 80000, None, app.db))[0]
    await release_claimed(app)
    async def fault(conn):
        await conn.execute(
            "CREATE TRIGGER reject_suppress BEFORE UPDATE ON deliveries "
            "WHEN NEW.status='suppressed' BEGIN SELECT RAISE(ABORT, 'disk fault'); END"
        )
    await app.writer.submit(fault)
    with pytest.raises(sqlite3.IntegrityError, match="disk fault"):
        await app.dispatcher.dispatch_once("2026-10-28T21:00:30+00:00")
    assert await delivery_status(app, deliveries[10]) == "leased"
    assert not [uid for uid, _ in public_v2_system[1].sent if uid == 10]


@pytest.mark.asyncio
async def test_newer_edit_fences_a_stale_suppression_decision(public_v2_system):
    from hunter.application.delivery_eligibility import (
        DeliveryEligibility,
        check_delivery_eligibility,
    )

    app, deliveries = await queued_event(public_v2_system)
    delivery = deliveries[10]
    assert (await edit_subscription(owner(), "sub_u10", 80000, None, app.db))[0]
    assert await check_delivery_eligibility(app.conn_mgr, delivery) == DeliveryEligibility.SUPPRESS
    assert (await edit_subscription(owner(), "sub_u10", 120000, None, app.db))[0]
    assert not await suppress_if_still_ineligible(app.delivery_repo, delivery)
    assert await delivery_status(app, delivery) == "leased"
