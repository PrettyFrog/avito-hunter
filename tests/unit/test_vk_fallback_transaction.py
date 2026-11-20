from datetime import UTC, datetime

import pytest
from tests.unit.test_delivery_repository import _insert_del

pytest_plugins = ("tests.unit.test_delivery_repository",)


@pytest.mark.asyncio
async def test_retry_and_vk_fallback_commit_together_and_are_idempotent(env):
    repo, writer, mgr, _ = env
    now = datetime.now(UTC).isoformat()
    await _insert_del(writer, "tg", now)

    async def bind(conn):
        await conn.execute(
            "INSERT INTO users (id, created_at, last_seen_at) VALUES (101, 't', 't')"
        )
        await conn.execute(
            "INSERT INTO channel_destinations (user_id, channel, address, verified, version) "
            "VALUES (101, 'vk', '1234', 1, 4)"
        )

    await writer.submit(bind)
    item = (await repo.claim_due(now))[0]
    assert await repo.retry_with_vk_fallback(item, 60)
    assert not await repo.retry_with_vk_fallback(item, 60)
    reader = await mgr.open_read_connection()
    try:
        rows = await (
            await reader.execute(
                "SELECT channel, status, destination_version FROM deliveries ORDER BY channel"
            )
        ).fetchall()
        assert [tuple(row) for row in rows] == [("telegram", "pending", 1), ("vk", "pending", 4)]
    finally:
        await mgr.close_reader(reader)


@pytest.mark.asyncio
async def test_fallback_insert_failure_rolls_back_telegram_retry(env):
    repo, writer, mgr, _ = env
    now = datetime.now(UTC).isoformat()
    await _insert_del(writer, "tg", now)

    async def bind_and_fault(conn):
        await conn.execute(
            "INSERT INTO users (id, created_at, last_seen_at) VALUES (101, 't', 't')"
        )
        await conn.execute(
            "INSERT INTO channel_destinations (user_id, channel, address, verified, version) "
            "VALUES (101, 'vk', '1234', 1, 2)"
        )
        await conn.execute(
            "CREATE TRIGGER reject_vk BEFORE INSERT ON deliveries WHEN NEW.channel='vk' "
            "BEGIN SELECT RAISE(ABORT, 'injected failure'); END"
        )

    await writer.submit(bind_and_fault)
    item = (await repo.claim_due(now))[0]
    with pytest.raises(Exception, match="injected failure"):
        await repo.retry_with_vk_fallback(item, 60)
    reader = await mgr.open_read_connection()
    try:
        row = await (
            await reader.execute("SELECT status, lease_token FROM deliveries WHERE id='tg'")
        ).fetchone()
        assert tuple(row) == ("leased", item.lease_token)
    finally:
        await mgr.close_reader(reader)


@pytest.mark.asyncio
async def test_retry_without_verified_vk_does_not_create_fallback(env):
    repo, writer, mgr, _ = env
    now = datetime.now(UTC).isoformat()
    await _insert_del(writer, "tg", now)
    item = (await repo.claim_due(now))[0]
    assert await repo.retry_with_vk_fallback(item, 60)
    reader = await mgr.open_read_connection()
    try:
        row = await reader.execute("SELECT COUNT(*) FROM deliveries WHERE channel='vk'")
        assert (await row.fetchone())[0] == 0
    finally:
        await mgr.close_reader(reader)
