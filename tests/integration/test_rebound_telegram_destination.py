from types import SimpleNamespace

import pytest

from hunter.domain.observations import ObservationBatch, ObservedListing, ParseOutcome, PriceState
from hunter.notify.public_telegram_sender import PublicTelegramSender

pytest_plugins = ("tests.integration.test_public_hunter",)


@pytest.mark.asyncio
async def test_new_event_after_chat_rebind_uses_current_destination_version(public_v2_system):
    bootstrap, _, query_id, generation = public_v2_system
    await bootstrap.dispatcher.stop()
    baseline = ObservationBatch(
        "slot_1",
        query_id,
        "2026-10-23T12:00:00Z",
        ParseOutcome.EMPTY,
        (),
        assignment_generation=generation,
    )
    await bootstrap.ingest_observation(baseline, request_sequence=1)
    await bootstrap.subscription_repo.bind_telegram_chat(10, 101)
    batch = ObservationBatch(
        "slot_1",
        query_id,
        "2026-10-23T12:00:30Z",
        ParseOutcome.OK,
        (
            ObservedListing(
                "new_item", "MacBook", 50000, PriceState.ACTIVE, "https://avito.ru/item/new_item"
            ),
        ),
        assignment_generation=generation,
    )
    result = await bootstrap.ingest_observation(batch, request_sequence=2)
    assert result.deliveries_created == 2
    row = await (
        await bootstrap.db._conn.execute(
            "SELECT destination_version FROM deliveries WHERE user_id = 10"
        )
    ).fetchone()
    assert row[0] == 2
    sender = PublicTelegramSender(bootstrap, None)
    assert (
        await sender._destination(SimpleNamespace(user_id=10, destination_version=row[0])) == "101"
    )


@pytest.mark.asyncio
async def test_rebind_does_not_retarget_previously_queued_delivery(public_v2_system):
    bootstrap, _, query_id, generation = public_v2_system
    await bootstrap.dispatcher.stop()
    await bootstrap.ingest_observation(
        ObservationBatch(
            "slot_1",
            query_id,
            "2026-10-23T12:00:00Z",
            ParseOutcome.EMPTY,
            (),
            assignment_generation=generation,
        ),
        request_sequence=1,
    )
    await bootstrap.ingest_observation(
        ObservationBatch(
            "slot_1",
            query_id,
            "2026-10-23T12:00:30Z",
            ParseOutcome.OK,
            (
                ObservedListing(
                    "old_item",
                    "MacBook",
                    50000,
                    PriceState.ACTIVE,
                    "https://avito.ru/item/old_item",
                ),
            ),
            assignment_generation=generation,
        ),
        request_sequence=2,
    )
    row = await (
        await bootstrap.db._conn.execute(
            "SELECT destination_version FROM deliveries WHERE user_id = 10"
        )
    ).fetchone()
    assert row[0] == 1
    await bootstrap.subscription_repo.bind_telegram_chat(10, 101)
    sender = PublicTelegramSender(bootstrap, None)
    assert (
        await sender._destination(SimpleNamespace(user_id=10, destination_version=row[0])) is None
    )
