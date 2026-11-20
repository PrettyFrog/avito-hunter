"""Unit tests for group price bounds widen/shrink, generation barrier, and price drops (T-505)."""

import asyncio
from pathlib import Path
import sqlite3
import pytest

from hunter.application.query_planner import PersistedQueryPlanner, Subscription
from hunter.application.umbrella import SyntheticItem
from hunter.storage.connection import StorageConfig, StorageConnectionManager
from hunter.storage.query_groups import QueryGroupRepository
from hunter.storage.writer import SQLiteWriterActor

SCHEMA = Path(__file__).resolve().parent.parent.parent / "src/hunter/core/schema.sql"


@pytest.fixture
async def env(tmp_path: Path):
    db_path = tmp_path / "bounds_test.db"
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.close()
    mgr = StorageConnectionManager(StorageConfig(db_path=db_path))
    writer = SQLiteWriterActor(mgr)
    await writer.start()
    repo = QueryGroupRepository(mgr, writer)
    planner = PersistedQueryPlanner(repo)
    yield planner, repo, writer, mgr, db_path
    await writer.stop()
    await mgr.drain_and_close()


@pytest.mark.asyncio
async def test_widen_waiting_when_no_transition_capacity(env) -> None:
    planner, repo, _, _, _ = env
    url = "https://www.avito.ru/moskva/audio?q=bose"
    sub = Subscription("sub_20k", 101, url, 20000)
    grp, _, _ = await planner.attach_subscription(sub)
    assert grp.max_price == 20000 and grp.generation == 1

    # Widen without transition capacity -> waiting
    upd_grp, ok, reason = await planner.widen_group_bounds(
        grp.id, 30000, url, transition_capacity=0
    )
    assert ok is False and "waiting" in reason
    cur = await repo.get_group(grp.id)
    assert cur is not None and cur.max_price == 20000 and cur.generation == 1


@pytest.mark.asyncio
async def test_widen_20_to_30_to_20_cycle_and_generation_barrier(env) -> None:
    planner, repo, writer, mgr, _ = env
    url = "https://www.avito.ru/moskva/audio?q=sony"
    sub1 = Subscription("sub_u1", 201, url, 20000)
    grp, _, _ = await planner.attach_subscription(sub1)

    # 1. Seed listing_state item in new price range (25000)
    async def _seed(c):
        await c.execute(
            "INSERT OR IGNORE INTO listing_state (avito_id, known_price, revision) "
            "VALUES ('item_25k', 25000, 1)"
        )

    await writer.submit(_seed)

    # 2. Widen 20k -> 30k with transition capacity
    upd_grp, ok, _ = await planner.widen_group_bounds(grp.id, 30000, url, transition_capacity=1)
    assert ok is True and upd_grp is not None and upd_grp.max_price == 30000
    assert upd_grp.generation == 2

    # Baseline for 25k item in new range must be recorded to prevent mass replay
    reader = await mgr.open_read_connection()
    cur = await reader.execute(
        "SELECT count(*) FROM subscription_seen "
        "WHERE subscription_id = 'sub_u1' AND avito_id = 'item_25k'"
    )
    assert (await cur.fetchone())[0] == 1
    await mgr.close_reader(reader)

    # 3. Old generation response (gen 1) is rejected at generation barrier
    items = [SyntheticItem("item_drop", "Sony XM4", 18000)]
    accepted, _, reason = await planner.filter_poll_response(
        grp.id, response_generation=1, items=items
    )
    assert accepted is False and "Stale response rejected" in reason

    # 4. Shrink 30k -> 20k preserves seen and bumps generation to 3
    shrunk_grp, changed = await planner.shrink_group_bounds(grp.id, url)
    assert changed is True and shrunk_grp is not None and shrunk_grp.max_price == 20000
    assert shrunk_grp.generation == 3

    # 5. Price drop within eligible ceiling is dispatched, not lost
    accepted_now, fanout, _ = await planner.filter_poll_response(
        grp.id, response_generation=3, items=items
    )
    assert accepted_now is True
    assert len(fanout["sub_u1"]) == 1
    assert fanout["sub_u1"][0].id == "item_drop"


@pytest.mark.asyncio
async def test_concurrent_attach_and_price_drop_during_bounds_transition(env) -> None:
    planner, repo, _, _, _ = env
    url = "https://www.avito.ru/moskva/audio?q=anker"
    sub_init = Subscription("sub_anker_20", 301, url, 20000)
    grp, _, _ = await planner.attach_subscription(sub_init)

    # Concurrently attach new subscription and widen bounds
    sub_new = Subscription("sub_anker_30", 302, url, 30000)
    attach_task = asyncio.create_task(planner.attach_subscription(sub_new))
    widen_task = asyncio.create_task(
        planner.widen_group_bounds(grp.id, 30000, url, transition_capacity=1)
    )

    (grp_att, mem_att, _), (grp_widen, ok_widen, _) = await asyncio.gather(attach_task, widen_task)
    assert ok_widen is True
    cur_grp = await repo.get_group(grp.id)
    assert cur_grp is not None and cur_grp.max_price == 30000 and cur_grp.generation == 2
