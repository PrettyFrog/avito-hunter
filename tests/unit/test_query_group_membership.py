"""Test persisted query group membership, atomic attach/detach, and tenant scoping."""

import asyncio
from pathlib import Path
import sqlite3
import pytest

from hunter.application.query_planner import PersistedQueryPlanner, Subscription
from hunter.storage.connection import StorageConfig, StorageConnectionManager
from hunter.storage.query_groups import AccessDeniedError, QueryGroupRepository
from hunter.storage.writer import SQLiteWriterActor

SCHEMA = Path(__file__).resolve().parent.parent.parent / "src/hunter/core/schema.sql"


@pytest.fixture
async def env(tmp_path: Path):
    db_path = tmp_path / "group_test.db"
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.close()
    mgr = StorageConnectionManager(StorageConfig(db_path=db_path))
    writer = SQLiteWriterActor(mgr)
    await writer.start()
    yield (
        PersistedQueryPlanner(QueryGroupRepository(mgr, writer)),
        QueryGroupRepository(mgr, writer),
        writer,
        mgr,
    )
    await writer.stop()
    await mgr.drain_and_close()


@pytest.mark.asyncio
async def test_concurrent_identical_subscriptions_create_one_physical_query(env) -> None:
    planner, repo, _, _ = env
    url = "https://www.avito.ru/moskva/tovary_dlya_kompyutera?q=rtx"
    sub1, sub2 = Subscription("sub_1", 101, url, 50000), Subscription("sub_2", 102, url, 50000)
    (grp1, mem1, _), (grp2, mem2, _) = await asyncio.gather(
        planner.attach_subscription(sub1), planner.attach_subscription(sub2)
    )
    assert (
        grp1.id == grp2.id and mem1.subscription_id == "sub_1" and mem2.subscription_id == "sub_2"
    )
    assert len(await repo.get_members(grp1.id)) == 2


@pytest.mark.asyncio
async def test_exact_sharing_precedes_compatible_price_sharing(env) -> None:
    planner, _, _, _ = env
    url = "https://www.avito.ru/moskva/audio_i_video?q=sony"
    grp_100k, _, _ = await planner.attach_subscription(
        Subscription("sub_100k", 101, url, 100000), allow_compatible_price_sharing=False
    )
    grp_50k, _, _ = await planner.attach_subscription(
        Subscription("sub_50k", 102, url, 50000), allow_compatible_price_sharing=False
    )
    assert grp_100k.id != grp_50k.id

    # Sub with 50k is compatible with 100k, but exact 50k exists -> joins exact 50k group
    grp_target, _, _ = await planner.attach_subscription(
        Subscription("sub_exact_50k", 103, url, 50000), allow_compatible_price_sharing=True
    )
    assert grp_target.id == grp_50k.id

    # Sub with 40k has no exact match; joins compatible 50k group (closest ceiling >= 40k)
    grp_compat, _, _ = await planner.attach_subscription(
        Subscription("sub_compat_40k", 104, url, 40000), allow_compatible_price_sharing=True
    )
    assert grp_compat.id == grp_50k.id


@pytest.mark.asyncio
async def test_two_users_isolation_and_tenant_scope(env) -> None:
    planner, repo, _, _ = env
    url = "https://www.avito.ru/moskva/noutbuki?q=thinkpad"
    grp, _, _ = await planner.attach_subscription(Subscription("sub_u1", 101, url, 30000))
    await planner.attach_subscription(Subscription("sub_u2", 102, url, 30000))

    with pytest.raises(AccessDeniedError):
        await planner.detach_subscription(grp.id, "sub_u1", user_id=102)
    assert len(await repo.get_members(grp.id)) == 2


@pytest.mark.asyncio
async def test_detach_preserves_peer_query(env) -> None:
    planner, repo, _, _ = env
    url = "https://www.avito.ru/moskva/telefony?q=iphone"
    grp, _, _ = await planner.attach_subscription(Subscription("sub_a", 201, url, 60000))
    await planner.attach_subscription(Subscription("sub_b", 202, url, 60000))

    assert await planner.detach_subscription(grp.id, "sub_a", user_id=201) is True
    assert await repo.get_group(grp.id) is not None
    assert len(await repo.get_members(grp.id)) == 1

    assert await planner.detach_subscription(grp.id, "sub_b", user_id=202) is True
    assert await repo.get_group(grp.id) is None


@pytest.mark.asyncio
async def test_replay_attach_is_idempotent_and_no_baseline_redispatch(env) -> None:
    planner, repo, writer, mgr = env

    async def _seed(c):
        await c.execute(
            "INSERT OR IGNORE INTO users (id, telegram_user_id, created_at) "
            "VALUES (301, 301, '2026-01-01T00:00:00')"
        )
        await c.execute(
            "INSERT OR IGNORE INTO queries (id, canonical_url) VALUES ('q1', 'https://www.avito.ru/moskva?q=rep')"
        )
        await c.execute(
            "INSERT OR IGNORE INTO subscriptions "
            "(id, user_id, query_id, max_price, manual_stopwords, enabled) "
            "VALUES ('sub_rep', 301, 'q1', 25000, '', 1)"
        )
        await c.execute(
            "INSERT OR IGNORE INTO listing_state (avito_id, known_price, revision) "
            "VALUES ('av_item_1', 20000, 1)"
        )
        await c.execute(
            "INSERT OR IGNORE INTO events "
            "(id, avito_id, revision, kind, observed_at, origin) "
            "VALUES ('ev_1', 'av_item_1', 1, 'NEW_LISTING', "
            "'2026-01-01T00:00:00', 'q1')"
        )

    await writer.submit(_seed)

    sub = Subscription("sub_rep", 301, "https://www.avito.ru/moskva?q=rep", 25000)
    grp1, mem1, created1 = await planner.attach_subscription(sub)
    assert created1 is True

    reader = await mgr.open_read_connection()
    cur = await reader.execute(
        "SELECT count(*) FROM subscription_seen "
        "WHERE subscription_id = 'sub_rep' AND avito_id = 'av_item_1'"
    )
    assert (await cur.fetchone())[0] == 1
    await mgr.close_reader(reader)

    grp2, mem2, created2 = await planner.attach_subscription(sub)
    assert created2 is False and grp1.id == grp2.id and mem1.id == mem2.id
    assert len(await repo.get_members(grp1.id)) == 1
