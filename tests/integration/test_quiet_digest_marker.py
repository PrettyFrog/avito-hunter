import json

import pytest

from hunter.domain.observations import ObservationBatch, ObservedListing, ParseOutcome, PriceState

pytest_plugins = ("tests.integration.test_public_hunter",)


@pytest.mark.asyncio
async def test_only_quiet_hours_deliveries_are_marked_for_digest(public_v2_system):
    app, _, query_id, generation = public_v2_system
    await app.dispatcher.stop()
    await app.db.update_user_quiet_hours(10, 1, 7)
    now = "2026-10-27T00:00:00Z"
    await app.ingest_observation(
        ObservationBatch(
            "slot_1", query_id, now, ParseOutcome.EMPTY, (), assignment_generation=generation
        ),
        request_sequence=1,
    )
    await app.ingest_observation(
        ObservationBatch(
            "slot_1",
            query_id,
            now,
            ParseOutcome.OK,
            (ObservedListing("qitem", "Item", 100, PriceState.ACTIVE, "https://avito.ru/item/1"),),
            assignment_generation=generation,
        ),
        request_sequence=2,
    )
    rows = await (
        await app.db._conn.execute(
            "SELECT user_id, payload, available_at FROM deliveries ORDER BY user_id"
        )
    ).fetchall()
    assert len(rows) == 2
    assert json.loads(rows[0][1]).get("quiet_digest") is True
    assert json.loads(rows[1][1]).get("quiet_digest") is not True
    assert rows[0][2].startswith("2026-10-27T04:00:00")
