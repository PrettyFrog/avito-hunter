from datetime import UTC, datetime

import pytest

from hunter.application.channel_binding import BindingError, ChannelBindingService
from hunter.domain.observations import ObservationBatch, ObservedListing, ParseOutcome, PriceState

pytest_plugins = ("tests.integration.test_public_hunter",)


@pytest.mark.asyncio
async def test_delete_user_removes_public_tenant_data_without_affecting_other_tenant(
    public_v2_system,
):
    app, _, query_id, generation = public_v2_system
    await app.dispatcher.stop()

    async def seed_identity(conn):
        await conn.execute(
            "UPDATE users SET telegram_user_id=1010, chat_id=1010, username='private' WHERE id=10"
        )

    await app.writer.submit(seed_identity)
    private = await app.subscription_service.add_subscription(
        user_id=10,
        url="https://avito.ru/moskva/noutbuki?q=private",
        max_price=100000,
        sub_id="private_u10",
    )
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
            (ObservedListing("item", "Item", 100, PriceState.ACTIVE, "https://avito.ru/item/1"),),
            assignment_generation=generation,
        ),
        request_sequence=2,
    )
    before = await (
        await app.db._conn.execute("SELECT COUNT(*) FROM deliveries WHERE user_id=10")
    ).fetchone()
    assert before[0] > 0
    service = ChannelBindingService("private-binding-secret-1234567890")
    unused_token = service.generate_token(10)
    await app.db.delete_user_data(10)
    for table in ("subscriptions", "deliveries", "recipient_events", "channel_destinations"):
        row = await (
            await app.db._conn.execute(f"SELECT COUNT(*) FROM {table} WHERE user_id=?", (10,))
        ).fetchone()
        assert row[0] == 0, table
    other = await (
        await app.db._conn.execute(
            "SELECT COUNT(*) FROM subscriptions WHERE user_id=20 AND query_id=?", (query_id,)
        )
    ).fetchone()
    assert other[0] == 1
    private_query = await (
        await app.db._conn.execute("SELECT COUNT(*) FROM queries WHERE id=?", (private.query_id,))
    ).fetchone()
    assert private_query[0] == 0
    row = await (
        await app.db._conn.execute(
            "SELECT status, telegram_user_id, chat_id FROM users WHERE id=10"
        )
    ).fetchone()
    assert tuple(row) == ("deleted", None, None)
    with pytest.raises(BindingError):
        await app.writer.submit(lambda conn: service.bind_from_peer(conn, 1234, unused_token))


@pytest.mark.asyncio
async def test_public_user_deletion_rolls_back_on_storage_failure(public_v2_system):
    app, _, query_id, _ = public_v2_system
    await app.dispatcher.stop()

    async def inject(conn):
        await conn.execute(
            "CREATE TRIGGER reject_deletion BEFORE DELETE ON channel_destinations "
            "WHEN OLD.user_id = 10 BEGIN SELECT RAISE(ABORT, 'injected deletion fault'); END"
        )

    await app.writer.submit(inject)
    with pytest.raises(Exception, match="injected deletion fault"):
        await app.db.delete_user_data(10)
    row = await (await app.db._conn.execute("SELECT status FROM users WHERE id=10")).fetchone()
    assert tuple(row) == ("active",)
    row = await (
        await app.db._conn.execute(
            "SELECT COUNT(*) FROM subscriptions WHERE user_id=10 AND query_id=?", (query_id,)
        )
    ).fetchone()
    assert row[0] == 1


@pytest.mark.asyncio
async def test_public_deletion_rejects_owner(public_v2_system):
    app, _, _, _ = public_v2_system
    await app.dispatcher.stop()

    async def make_owner(conn):
        await conn.execute("UPDATE users SET role='owner' WHERE id=10")

    await app.writer.submit(make_owner)
    with pytest.raises(ValueError, match="Owner cannot be deleted"):
        await app.db.delete_user_data(10)
    row = await (await app.db._conn.execute("SELECT status FROM users WHERE id=10")).fetchone()
    assert row[0] == "active"
