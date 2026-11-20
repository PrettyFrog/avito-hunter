"""Regression test reproducing BUG-001: NoActiveSlotCapacityError on SUSPECT slot (<= 400 lines)."""

from __future__ import annotations

import sqlite3
import time
import pytest

from hunter.domain.slots import SlotHealth, SlotRole
from hunter.storage.connection import StorageConfig, StorageConnectionManager
from hunter.storage.slot_state import SlotRepository
from hunter.storage.subscriptions import SubscriptionActor, SubscriptionRepository
from hunter.storage.writer import SQLiteWriterActor


@pytest.mark.asyncio
async def test_add_subscription_succeeds_when_slot_is_suspect(tmp_path) -> None:
    db_path = tmp_path / "test_bug_001.db"
    con = sqlite3.connect(db_path)
    schema_sql = open("src/hunter/core/schema.sql", encoding="utf-8").read()
    con.executescript(schema_sql)
    con.close()

    mgr = StorageConnectionManager(StorageConfig(db_path=db_path))
    writer = SQLiteWriterActor(mgr)
    await writer.start()
    slot_repo = SlotRepository(mgr, writer)
    sub_repo = SubscriptionRepository(mgr, writer)

    try:
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        # Create user
        async def _seed_user(c):
            await c.execute(
                "INSERT INTO users (id, telegram_user_id, chat_id, username, role, status, created_at) "
                "VALUES (10, 10, 10, 'u10', 'user', 'active', ?)", (now_iso,)
            )
        await writer.submit(_seed_user)

        # Register slot and set health to suspect (transient 429)
        await slot_repo.register_slot("slot-home", "direct", role=SlotRole.ACTIVE)
        await slot_repo.set_health("slot-home", SlotHealth.SUSPECT)

        # Adding subscription should succeed without NoActiveSlotCapacityError
        actor = SubscriptionActor(user_id=10, is_owner=False)
        sub = await sub_repo.create(
            actor=actor,
            sub_id="sub_test_001",
            target_user=10,
            qid="q_test_001",
            pmax=50000,
            url="https://www.avito.ru/moskva/audio_i_video/test_12345",
            auto_assign=True,
            max_queries_per_slot=4,
        )
        assert sub is not None
    finally:
        await writer.stop()
        await mgr.drain_and_close()
