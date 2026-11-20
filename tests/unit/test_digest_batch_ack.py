from dataclasses import replace
from datetime import UTC, datetime

import pytest
from tests.unit.test_delivery_repository import _insert_del

pytest_plugins = ("tests.unit.test_delivery_repository",)


async def claimed_pair(env):
    repo, writer, _, _ = env
    now = datetime.now(UTC).isoformat()
    await _insert_del(writer, "one", now)
    await _insert_del(writer, "two", now)
    return await repo.claim_due(now)


async def states(mgr):
    reader = await mgr.open_read_connection()
    try:
        rows = await (
            await reader.execute("SELECT id, status, message_id FROM deliveries ORDER BY id")
        ).fetchall()
        return [tuple(row) for row in rows]
    finally:
        await mgr.close_reader(reader)


@pytest.mark.asyncio
async def test_digest_ack_commits_all_members_once(env):
    repo, _, mgr, _ = env
    items = await claimed_pair(env)
    assert await repo.complete_digest(items, "message-42")
    assert await states(mgr) == [("one", "sent", "message-42"), ("two", "sent", "message-42")]
    assert not await repo.complete_digest(items, "again")


@pytest.mark.asyncio
async def test_digest_ack_rejects_stale_lease_and_cross_user_without_partial_update(env):
    repo, _, mgr, _ = env
    items = await claimed_pair(env)
    assert not await repo.complete_digest([items[0], replace(items[1], lease_token="stale")], "m")
    assert not await repo.complete_digest([items[0], replace(items[1], user_id=999)], "m")
    assert not await repo.complete_digest([items[0], items[0]], "m")
    assert all(row[1] == "leased" for row in await states(mgr))


@pytest.mark.asyncio
async def test_digest_ack_rolls_back_all_members_on_storage_fault(env):
    repo, writer, mgr, _ = env
    items = await claimed_pair(env)

    async def inject(conn):
        await conn.execute(
            "CREATE TRIGGER reject_second BEFORE UPDATE ON deliveries "
            "WHEN NEW.id='two' AND NEW.status='sent' "
            "BEGIN SELECT RAISE(ABORT, 'injected ack fault'); END"
        )

    await writer.submit(inject)
    with pytest.raises(Exception, match="injected ack fault"):
        await repo.complete_digest(items, "m")
    assert all(row[1] == "leased" for row in await states(mgr))
