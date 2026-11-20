"""Unit tests for tenant-scoped subscription repository (T-471)."""

from pathlib import Path
import sqlite3
import pytest
import aiosqlite

from hunter.storage.connection import StorageConfig, StorageConnectionManager
from hunter.storage.writer import SQLiteWriterActor
from hunter.storage.subscriptions import (
    AccessDeniedError,
    StaleSubscriptionVersionError,
    SubscriptionActor,
    SubscriptionRepository,
    TombstonedSubscriptionError,
)

SCHEMA = Path(__file__).resolve().parent.parent.parent / "src/hunter/core/schema.sql"


@pytest.fixture
async def env(tmp_path: Path):
    db_path = tmp_path / "sub_repo.db"
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.close()
    mgr = StorageConnectionManager(StorageConfig(db_path=db_path))
    writer = SQLiteWriterActor(mgr)
    await writer.start()
    yield SubscriptionRepository(mgr, writer), writer, mgr, db_path
    await writer.stop()
    await mgr.drain_and_close()


@pytest.mark.asyncio
async def test_two_users_isolation_and_owner_hierarchy(env) -> None:
    repo, _, _, _ = env
    u1, u2, owner = (
        SubscriptionActor(101),
        SubscriptionActor(102),
        SubscriptionActor(1, is_owner=True),
    )
    rec1 = await repo.create(u1, "sub_101", 101, "q_mac", 50000, "broken")
    assert rec1.id == "sub_101" and rec1.version == 1

    # User 102 denied access to user 101's subscription
    with pytest.raises(AccessDeniedError):
        await repo.get(u2, "sub_101")
    with pytest.raises(AccessDeniedError):
        await repo.update(u2, "sub_101", 1, max_price=60000)
    with pytest.raises(AccessDeniedError):
        await repo.delete(u2, "sub_101")

    assert len(await repo.list_for_actor(u2)) == 0
    assert len(await repo.list_for_actor(u1)) == 1
    assert (await repo.get(owner, "sub_101")).id == "sub_101"
    assert len(await repo.list_for_actor(owner)) == 1


@pytest.mark.asyncio
async def test_stale_version_update_and_no_replay_seen(env) -> None:
    repo, writer, mgr, _ = env
    u1 = SubscriptionActor(201)
    await repo.create(u1, "sub_v", 201, "q_v", 40000)
    await writer.submit(
        lambda c: c.execute(
            "INSERT INTO subscription_seen (subscription_id, avito_id, last_revision) "
            "VALUES ('sub_v', 'lot_1', 1)"
        )
    )

    u = await repo.update(u1, "sub_v", expected_version=1, max_price=45000)
    assert u.version == 2 and u.max_price == 45000
    with pytest.raises(StaleSubscriptionVersionError):
        await repo.update(u1, "sub_v", expected_version=1, max_price=50000)

    reader = await mgr.open_read_connection()
    assert (
        await (
            await reader.execute(
                "SELECT count(*) FROM subscription_seen WHERE subscription_id = 'sub_v'"
            )
        ).fetchone()
    )[0] == 1
    await mgr.close_reader(reader)


@pytest.mark.asyncio
async def test_delete_pending_deliveries_and_durable_restart_tombstone(env) -> None:
    repo, writer, mgr, db_path = env
    u1 = SubscriptionActor(301)
    await repo.create(u1, "sub_del", 301, "q_del", 30000)

    async def _seed(c: aiosqlite.Connection):
        await c.execute(
            "INSERT INTO events (id, avito_id, revision, kind, observed_at, origin) "
            "VALUES ('ev_del', 'lot_del', 1, 'NEW_LISTING', 't', 'q_del')"
        )
        await c.execute(
            "INSERT INTO deliveries (id, event_id, user_id, channel, status, available_at) "
            "VALUES ('del_1', 'ev_del', 301, 'telegram', 'pending', 't')"
        )

    await writer.submit(_seed)

    await repo.delete(u1, "sub_del")
    reader = await mgr.open_read_connection()
    assert (
        await (
            await reader.execute(
                "SELECT count(*) FROM deliveries WHERE user_id = 301 AND status = 'pending'"
            )
        ).fetchone()
    )[0] == 0
    await mgr.close_reader(reader)

    assert await repo.is_tombstoned("sub_del") is True
    with pytest.raises(TombstonedSubscriptionError):
        await repo.create(u1, "sub_del", 301, "q_del", 30000)

    # Durable restart tombstone check
    mgr2 = StorageConnectionManager(StorageConfig(db_path=db_path))
    writer2 = SQLiteWriterActor(mgr2)
    await writer2.start()
    assert await SubscriptionRepository(mgr2, writer2).is_tombstoned("sub_del") is True
    await writer2.stop()
    await mgr2.drain_and_close()
