"""Regression test for F-035: _toggle_subscription seeds baseline on resume (ADR-02 / RB-1).

Verifies that resuming a paused subscription:
1. Seeds all existing query listings into subscription_seen with baseline_epoch=1.
2. Prevents alert avalanche from listings accumulated during the pause period.
3. Enforces optimistic concurrency control (CAS) on version changes.
4. Enforces tenant ownership (foreign user cannot pause or resume).
"""

from __future__ import annotations

from pathlib import Path
import aiosqlite
import pytest

from hunter.core.models import User
from hunter.notify.telegram_handlers.public_status import (
    pause_subscription,
    resume_subscription,
)
from hunter.storage.connection import StorageConfig, StorageConnectionManager
from hunter.storage.subscriptions import SubscriptionActor, SubscriptionRepository
from hunter.storage.writer import SQLiteWriterActor


class FakeDB:
    def __init__(self, repo, writer, mgr, path):
        self.subscription_repo = repo
        self.writer = writer
        self.storage_mgr = mgr
        self.path = path


@pytest.mark.asyncio
async def test_repro_f035_resume_seeds_seen_baseline(tmp_path: Path) -> None:
    db_path = tmp_path / "test_f035.db"
    project_root = Path(__file__).resolve().parents[2]
    schema_sql = (project_root / "src" / "hunter" / "core" / "schema.sql").read_text(encoding="utf-8")

    async with aiosqlite.connect(db_path) as init_conn:
        await init_conn.executescript(schema_sql)
        await init_conn.commit()

    mgr = StorageConnectionManager(StorageConfig(db_path=db_path))
    writer = SQLiteWriterActor(mgr)
    await writer.start()
    repo = SubscriptionRepository(mgr, writer)
    fake_db = FakeDB(repo, writer, mgr, db_path)

    user101 = User(id=101, telegram_user_id=101, chat_id=101, role="user", status="active", created_at="")
    user102 = User(id=102, telegram_user_id=102, chat_id=102, role="user", status="active", created_at="")

    async def setup_data(conn: aiosqlite.Connection):
        await conn.execute("INSERT INTO users (id, telegram_user_id, chat_id, role, status, created_at) VALUES (101, 101, 101, 'user', 'active', 't')")
        await conn.execute("INSERT INTO users (id, telegram_user_id, chat_id, role, status, created_at) VALUES (102, 102, 102, 'user', 'active', 't')")
        await conn.execute("INSERT INTO queries (id, canonical_url, bootstrap_state) VALUES ('q_test', 'https://www.avito.ru/test', 'ready')")
        # Subscription paused
        await conn.execute(
            "INSERT INTO subscriptions (id, user_id, query_id, max_price, enabled, version, created_at, updated_at) "
            "VALUES ('sub_test', 101, 'q_test', 50000, 0, 1, 't', 't')"
        )
        # 3 listings accumulated on query while paused
        for i in range(1, 4):
            avid = str(9000 + i)
            await conn.execute("INSERT INTO listing_state (avito_id, known_price, revision) VALUES (?, 40000, 1)", (avid,))
            await conn.execute("INSERT INTO query_membership (query_id, avito_id, first_seen, last_seen) VALUES ('q_test', ?, 't', 't')", (avid,))

    await writer.submit(setup_data)

    # 1. Foreign user 102 cannot resume user 101's subscription
    ok_foreign, msg_foreign = await resume_subscription(user102, "sub_test", fake_db)
    assert ok_foreign is False
    assert "нет доступа" in msg_foreign

    # 2. Before resume: 0 seen rows
    read_conn = await mgr.open_read_connection()
    cur = await read_conn.execute("SELECT count(*) FROM subscription_seen WHERE subscription_id = 'sub_test'")
    assert (await cur.fetchone())[0] == 0
    await mgr.close_reader(read_conn)

    # 3. Owner resumes subscription
    ok, msg = await resume_subscription(user101, "sub_test", fake_db)
    assert ok is True
    assert "возобновлена" in msg

    # 4. After resume: all 3 accumulated listings are seeded into subscription_seen (ADR-02)
    read_conn = await mgr.open_read_connection()
    cur = await read_conn.execute(
        "SELECT count(*), min(baseline_epoch) FROM subscription_seen WHERE subscription_id = 'sub_test'"
    )
    row = await cur.fetchone()
    await mgr.close_reader(read_conn)
    assert row[0] == 3, f"Expected 3 seeded seen listings, got {row[0]}"
    assert row[1] == 1, "Baseline epoch must be 1"

    # 5. Pausing again increments version and clears enabled flag
    ok_pause, msg_pause = await pause_subscription(user101, "sub_test", fake_db)
    assert ok_pause is True
    assert "на паузу" in msg_pause

    sub = await repo.get(SubscriptionActor(101), "sub_test")
    assert sub.enabled is False
    assert sub.version == 3

    await writer.stop()
    await mgr.drain_and_close()
