from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from hunter.domain.observations import ObservationBatch, ObservedListing, ParseOutcome, PriceState

pytest_plugins = ("tests.integration.test_public_hunter",)


@pytest.mark.asyncio
async def test_blocked_user_does_not_receive_pending_delivery_and_can_resume(public_v2_system):
    app, sender, query_id, generation = public_v2_system
    await app.dispatcher.stop()
    now = datetime.now(UTC).isoformat()
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
            (ObservedListing("lot", "Item", 100, PriceState.ACTIVE, "https://avito.ru/item/1"),),
            assignment_generation=generation,
        ),
        request_sequence=2,
    )

    async def block(conn):
        await conn.execute("UPDATE users SET status='blocked_by_owner' WHERE id=10")

    await app.writer.submit(block)
    await app.dispatcher.dispatch_once(datetime.now(UTC).isoformat())
    assert [uid for uid, _ in sender.sent] == [20]
    row = await (
        await app.db._conn.execute("SELECT status FROM deliveries WHERE user_id=10")
    ).fetchone()
    assert row[0] == "pending"

    async def resume(conn):
        await conn.execute("UPDATE users SET status='active' WHERE id=10")
        await conn.execute("UPDATE deliveries SET available_at=? WHERE user_id=10", (now,))

    await app.writer.submit(resume)
    await app.dispatcher.dispatch_once(datetime.now(UTC).isoformat())
    assert [uid for uid, _ in sender.sent] == [20, 10]


@pytest.mark.asyncio
async def test_user_status_read_failure_does_not_send(public_v2_system, monkeypatch):
    app, sender, query_id, generation = public_v2_system
    await app.dispatcher.stop()
    now = datetime.now(UTC).isoformat()
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
            (ObservedListing("fault", "Item", 100, PriceState.ACTIVE, "https://avito.ru/item/2"),),
            assignment_generation=generation,
        ),
        request_sequence=2,
    )
    monkeypatch.setattr(
        app.delivery_repo, "is_user_active", AsyncMock(side_effect=OSError("fault"))
    )
    with pytest.raises(OSError, match="fault"):
        await app.dispatcher.dispatch_once(datetime.now(UTC).isoformat())
    assert sender.sent == []
