"""An inactive assignment must not consume a slot position forever."""
import sqlite3

import pytest

from hunter.domain.observations import ObservationBatch, ParseOutcome

pytest_plugins = ("tests.integration.test_public_hunter",)


@pytest.mark.asyncio
async def test_subscription_can_reuse_an_inactive_assignment(public_v2_system):
    app, _, qid, generation = public_v2_system
    await app.dispatcher.stop()
    await app.writer.submit(lambda c: c.execute(
        "UPDATE assignments SET active = 0 WHERE query_id = ?", (qid,)
    ))
    created = await app.subscription_service.add_subscription(
        10, "https://avito.ru/moskva?q=car", 10000, sub_id="sub_new", name="Машина"
    )
    row = await (await app.db._conn.execute(
        "SELECT active FROM assignments WHERE query_id = ?", (created.query_id,)
    )).fetchone()
    assert row and row[0] == 1
    stale = await app.ingest_observation(
        ObservationBatch(slot_id="slot_1", query_id=qid, observed_at="2026-10-27T00:00:00+00:00",
                         outcome=ParseOutcome.EMPTY, listings=(), assignment_generation=generation),
        request_sequence=50,
    )
    assert stale.stale_assignment is True


@pytest.mark.asyncio
async def test_reuse_fault_rolls_back_slot_generation(public_v2_system):
    app, _, qid, _ = public_v2_system
    await app.dispatcher.stop()
    await app.writer.submit(lambda c: c.execute(
        "UPDATE assignments SET active = 0 WHERE query_id = ?", (qid,)
    ))
    async def install_fault(c):
        await c.execute("CREATE TRIGGER fail_reuse BEFORE UPDATE ON assignments "
                        "WHEN NEW.active = 1 BEGIN SELECT RAISE(ABORT, 'reuse fault'); END")
    await app.writer.submit(install_fault)
    before = await (await app.db._conn.execute(
        "SELECT active_generation FROM slots WHERE id = 'slot_1'"
    )).fetchone()
    with pytest.raises(sqlite3.IntegrityError, match="reuse fault"):
        await app.subscription_service.add_subscription(
            10, "https://avito.ru/moskva?q=car", 10000, sub_id="sub_new", name="Машина"
        )
    after = await (await app.db._conn.execute(
        "SELECT active_generation FROM slots WHERE id = 'slot_1'"
    )).fetchone()
    assert before[0] == after[0]
    assert (await (await app.db._conn.execute(
        "SELECT count(*) FROM subscriptions WHERE id='sub_new'"
    )).fetchone())[0] == 0
