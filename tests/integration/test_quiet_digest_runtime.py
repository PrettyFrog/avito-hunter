"""Exercise quiet-hour digests through the real public outbox dispatcher."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hunter.domain.observations import ObservationBatch, ObservedListing, ParseOutcome, PriceState
from hunter.notify.public_telegram_sender import PublicTelegramSender

pytest_plugins = ("tests.integration.test_public_hunter",)


async def prepare(system):
    app, _, query_id, generation = system
    await app.dispatcher.stop()
    await app.db.update_user_quiet_hours(10, 1, 7)
    def batch(sequence, listings):
        return ObservationBatch(
            slot_id="slot_1", query_id=query_id, observed_at="2026-10-27T00:00:00+00:00",
            outcome=ParseOutcome.OK, listings=listings, assignment_generation=generation,
        )
    baseline = ObservedListing(avito_id="baseline", title="Baseline", price=70000,
                               price_state=PriceState.ACTIVE, url="https://avito.ru/item/0")
    await app.ingest_observation(batch(1, (baseline,)), request_sequence=1)
    lots = tuple(ObservedListing(
        avito_id=f"lot_{i}", title=f"MacBook {i}", price=70000,
        price_state=PriceState.ACTIVE, url=f"https://avito.ru/item/{i}",
    ) for i in (1, 2))
    result = await app.ingest_observation(batch(2, lots), request_sequence=2)
    assert result.deliveries_created == 4
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=42)))
    channel = SimpleNamespace(_client=AsyncMock(return_value=bot))
    app.senders["telegram"] = PublicTelegramSender(app, channel)
    return app, bot


@pytest.mark.asyncio
async def test_quiet_events_are_grouped_but_other_user_is_not(public_v2_system):
    app, bot = await prepare(public_v2_system)
    await app.dispatcher.dispatch_once("2026-10-27T04:00:01+00:00")
    calls = bot.send_message.call_args_list
    quiet = [c for c in calls if str(c.args[0]) == "100"]
    normal = [c for c in calls if str(c.args[0]) == "200"]
    assert len(quiet) == 1
    assert len(normal) == 2
    assert "https://avito.ru/item/1" in quiet[0].args[1]
    assert "https://avito.ru/item/2" in quiet[0].args[1]
    rows = await (await app.db._conn.execute(
        "SELECT user_id, status, message_id FROM deliveries ORDER BY user_id"
    )).fetchall()
    assert len(rows) == 4 and all(row[1] == "sent" and row[2] == "42" for row in rows)


@pytest.mark.asyncio
async def test_digest_ack_failure_recovers_without_partial_ack(public_v2_system, monkeypatch):
    app, bot = await prepare(public_v2_system)
    original = app.delivery_repo.complete_digest
    monkeypatch.setattr(
        app.delivery_repo, "complete_digest", AsyncMock(side_effect=OSError("disk"))
    )
    with pytest.raises(OSError):
        await app.dispatcher.dispatch_once("2026-10-27T04:00:01+00:00")
    rows = await (await app.db._conn.execute(
        "SELECT status FROM deliveries WHERE user_id = 10"
    )).fetchall()
    assert len(rows) == 2 and all(row[0] == "leased" for row in rows)
    monkeypatch.setattr(app.delivery_repo, "complete_digest", original)
    async def expire(conn):
        await conn.execute("UPDATE deliveries SET lease_until = '2000-01-01' WHERE user_id = 10")
    await app.writer.submit(expire)
    await app.dispatcher.dispatch_once("2026-10-27T04:02:01+00:00")
    rows = await (await app.db._conn.execute(
        "SELECT status FROM deliveries WHERE user_id = 10"
    )).fetchall()
    assert all(row[0] == "sent" for row in rows)
    assert len([c for c in bot.send_message.call_args_list if str(c.args[0]) == "100"]) == 2
