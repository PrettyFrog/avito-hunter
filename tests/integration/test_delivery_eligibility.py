"""Revalidate queued events against the tenant's current subscriptions."""

from unittest.mock import AsyncMock

import pytest

from hunter.application.delivery_eligibility import DeliveryEligibility, check_delivery_eligibility
from hunter.core.models import User
from hunter.domain.observations import ObservationBatch, ObservedListing, ParseOutcome, PriceState
from hunter.notify.telegram_handlers.public_status import edit_subscription

pytest_plugins = ("tests.integration.test_public_hunter",)


async def queued_event(system):
    app, _, qid, generation = system
    await app.dispatcher.stop()

    def batch(listings):
        return ObservationBatch(
            slot_id="slot_1",
            query_id=qid,
            observed_at="2026-10-28T21:00:00+00:00",
            outcome=ParseOutcome.OK,
            listings=listings,
            assignment_generation=generation,
        )

    def item(number, price):
        return ObservedListing(
            avito_id=f"lot{number}",
            title="MacBook",
            price=price,
            price_state=PriceState.ACTIVE,
            url=f"https://avito.ru/item/{number}",
        )

    await app.ingest_observation(batch((item(0, 70000),)), request_sequence=1)
    result = await app.ingest_observation(
        batch((item(0, 70000), item(1, 90000))), request_sequence=2
    )
    assert result.deliveries_created == 2
    claimed = await app.delivery_repo.claim_due("2026-10-28T21:00:30+00:00")
    assert len(claimed) == 2
    return app, {item.user_id: item for item in claimed}


@pytest.mark.asyncio
async def test_changed_ceiling_applies_only_to_its_owner(public_v2_system):
    app, deliveries = await queued_event(public_v2_system)
    user = User(id=10, telegram_user_id=10, chat_id=10, role="user", created_at="")
    from hunter.application.delivery_eligibility import _check_in_snapshot

    reader = await app.conn_mgr.open_read_connection()
    try:
        assert await _check_in_snapshot(reader, deliveries[10]) == DeliveryEligibility.ELIGIBLE
    finally:
        await app.conn_mgr.close_reader(reader)
    assert (
        await check_delivery_eligibility(app.conn_mgr, deliveries[10])
        == DeliveryEligibility.ELIGIBLE
    )
    assert (await edit_subscription(user, "sub_u10", 80000, None, app.db))[0]
    assert (
        await check_delivery_eligibility(app.conn_mgr, deliveries[10])
        == DeliveryEligibility.SUPPRESS
    )
    assert (
        await check_delivery_eligibility(app.conn_mgr, deliveries[20])
        == DeliveryEligibility.ELIGIBLE
    )


@pytest.mark.asyncio
async def test_other_search_remains_possible_until_matching_query_seen(public_v2_system):
    app, deliveries = await queued_event(public_v2_system)
    user = User(id=10, telegram_user_id=10, chat_id=10, role="user", created_at="")
    assert (await edit_subscription(user, "sub_u10", 80000, None, app.db))[0]
    sub = await app.subscription_service.add_subscription(
        10, "https://avito.ru/moskva?q=mac", 120000, sub_id="sub_other"
    )
    assert (
        await check_delivery_eligibility(app.conn_mgr, deliveries[10]) == DeliveryEligibility.DEFER
    )

    async def mark_seen(conn):
        await conn.execute(
            "UPDATE queries SET bootstrap_state = 'ready' WHERE id = ?", (sub.query_id,)
        )
        await conn.execute(
            "INSERT INTO query_membership(query_id,avito_id,first_seen,last_seen) "
            "VALUES (?, 'lot1', '2026-10-28T21:00:00+00:00', '2026-10-28T21:00:00+00:00')",
            (sub.query_id,),
        )

    await app.writer.submit(mark_seen)
    assert (
        await check_delivery_eligibility(app.conn_mgr, deliveries[10])
        == DeliveryEligibility.ELIGIBLE
    )


@pytest.mark.asyncio
async def test_current_stopwords_and_read_failure_fail_closed(public_v2_system, monkeypatch):
    app, deliveries = await queued_event(public_v2_system)
    user = User(id=10, telegram_user_id=10, chat_id=10, role="user", created_at="")
    assert (await edit_subscription(user, "sub_u10", None, "macbook", app.db))[0]
    assert (
        await check_delivery_eligibility(app.conn_mgr, deliveries[10])
        == DeliveryEligibility.SUPPRESS
    )
    monkeypatch.setattr(
        app.conn_mgr, "open_read_connection", AsyncMock(side_effect=OSError("disk"))
    )
    assert (
        await check_delivery_eligibility(app.conn_mgr, deliveries[10]) == DeliveryEligibility.DEFER
    )


@pytest.mark.asyncio
async def test_mismatched_payload_and_unknown_event_defer(public_v2_system):
    from dataclasses import replace

    app, deliveries = await queued_event(public_v2_system)
    delivery = deliveries[10]
    forged = replace(delivery, payload={**delivery.payload, "new_price": 1})
    missing = replace(delivery, event_id="unknown")
    assert await check_delivery_eligibility(app.conn_mgr, forged) == DeliveryEligibility.DEFER
    assert await check_delivery_eligibility(app.conn_mgr, missing) == DeliveryEligibility.DEFER


@pytest.mark.asyncio
async def test_current_keywords_suppress_delivery_when_not_matching(public_v2_system):
    app, deliveries = await queued_event(public_v2_system)
    from hunter.storage.subscriptions import SubscriptionActor

    await app.subscription_repo.update(SubscriptionActor(10), "sub_u10", expected_version=1, keywords="thinkpad")
    assert await check_delivery_eligibility(app.conn_mgr, deliveries[10]) == DeliveryEligibility.SUPPRESS
    await app.subscription_repo.update(SubscriptionActor(10), "sub_u10", expected_version=2, keywords="macbook")
    assert await check_delivery_eligibility(app.conn_mgr, deliveries[10]) == DeliveryEligibility.ELIGIBLE
