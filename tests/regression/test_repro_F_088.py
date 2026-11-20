"""Regression test for F-088: User deletion does not pause or deactivate active public subscriptions.

Verifies that:
1. When a user is marked deleted or calls /delete_me, _assignment_poll_state pauses the query
   even if a subscription row still exists or had enabled=1.
2. delete_user_data deletes user subscriptions and deactivates orphaned assignments in slots.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
import pytest

from hunter.application.public_poll_runtime import QueryAssignment, _assignment_poll_state
from hunter.core.db import Database
from hunter.notify.telegram_router_parts.setup_telegram_router_cmd_delete_me_53 import (
    handle_delete_me_confirm_callback,
)


class DummyConnMgr:
    def __init__(self, db_path: Path):
        self.db_path = str(db_path)

    async def open_read_connection(self):
        import aiosqlite
        return await aiosqlite.connect(self.db_path)

    async def close_reader(self, conn):
        await conn.close()


@pytest.mark.asyncio
async def test_repro_f088_poll_state_ignores_deleted_users(tmp_path: Path):
    db_file = tmp_path / "hunter.db"
    con = sqlite3.connect(db_file)
    con.executescript("""
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            role TEXT,
            status TEXT,
            telegram_user_id INTEGER,
            chat_id INTEGER,
            username TEXT,
            display_name TEXT
        );
        CREATE TABLE slots (
            id TEXT PRIMARY KEY,
            health_state TEXT
        );
        CREATE TABLE assignments (
            slot_id TEXT,
            query_id TEXT,
            generation INTEGER,
            active INTEGER
        );
        CREATE TABLE subscriptions (
            id TEXT PRIMARY KEY,
            user_id INTEGER,
            query_id TEXT,
            enabled INTEGER
        );

        INSERT INTO slots VALUES ('slot-1', 'ready');
        INSERT INTO assignments VALUES ('slot-1', 'q_iphone', 1, 1);
        -- User 2 is deleted, but their subscription row had enabled=1
        INSERT INTO users VALUES (2, 'user', 'deleted', NULL, NULL, NULL, NULL);
        INSERT INTO subscriptions VALUES ('sub_1', 2, 'q_iphone', 1);
    """)
    con.commit()
    con.close()

    asg = QueryAssignment(
        slot_id="slot-1",
        endpoint="https://example.com",
        query_id="q_iphone",
        canonical_url="https://example.com/q",
        generation=1,
        position=0,
    )
    conn_mgr = DummyConnMgr(db_file)

    state = await _assignment_poll_state(conn_mgr, asg)
    # The bug: state was 'current' because it didn't check user.status == 'active'.
    # Expected: must be 'paused' because user 2 is deleted.
    assert state == "paused"


@pytest.mark.asyncio
async def test_repro_f088_delete_user_data_deactivates_assignments(tmp_path: Path):
    db = Database(tmp_path / "hunter.db")
    await db.connect()

    # Setup user, query, slot, assignment, subscription
    uid = await db.create_user(telegram_user_id=2002, chat_id=2002, role="user", status="active")
    conn = await db.connect()
    await conn.execute("INSERT INTO queries (id, canonical_url) VALUES ('q_iphone', 'https://example.com/q')")
    await conn.execute("INSERT INTO slots (id, endpoint, cookie_namespace, health_state) VALUES ('slot-1', 'https://example.com', 'ns1', 'ready')")
    await conn.execute("INSERT INTO assignments (id, slot_id, query_id, position, generation, active) VALUES ('asg-1', 'slot-1', 'q_iphone', 0, 1, 1)")
    await conn.execute("INSERT INTO subscriptions (id, user_id, query_id, max_price, name, enabled) VALUES ('sub_iphone', ?, 'q_iphone', 1000, 'iPhone', 1)", (uid,))
    await conn.commit()

    # Call delete_user_data on user
    await db.delete_user_data(uid)

    # Subscriptions for user must be purged
    cur = await conn.execute("SELECT count(*) FROM subscriptions WHERE user_id = ?", (uid,))
    assert (await cur.fetchone())[0] == 0

    # Orphaned assignment for q_iphone must be deactivated or deleted
    cur_asg = await conn.execute("SELECT active FROM assignments WHERE query_id = 'q_iphone'")
    row = await cur_asg.fetchone()
    assert row is None or row[0] == 0


@pytest.mark.asyncio
async def test_repro_f088_delete_public_user_deactivates_assignments(tmp_path: Path):
    from hunter.storage.user_deletion import delete_public_user
    import aiosqlite

    db_file = tmp_path / "hunter_public.db"
    db = Database(db_file)
    await db.connect()

    conn = await aiosqlite.connect(str(db_file))
    uid = await db.create_user(telegram_user_id=3003, chat_id=3003, role="user", status="active")
    await conn.execute("INSERT INTO queries (id, canonical_url) VALUES ('q_iphone_pub', 'https://example.com/q2')")
    await conn.execute("INSERT INTO slots (id, endpoint, cookie_namespace, health_state) VALUES ('slot-1', 'https://example.com', 'ns1', 'ready')")
    await conn.execute("INSERT INTO assignments (id, slot_id, query_id, position, generation, active) VALUES ('asg-2', 'slot-1', 'q_iphone_pub', 0, 1, 1)")
    await conn.execute("INSERT INTO subscriptions (id, user_id, query_id, max_price, name, enabled) VALUES ('sub_iphone_pub', ?, 'q_iphone_pub', 1000, 'iPhone', 1)", (uid,))
    await conn.commit()

    # Call delete_public_user directly
    await delete_public_user(conn, uid)
    await conn.commit()

    # Subscriptions must be deleted
    cur = await conn.execute("SELECT count(*) FROM subscriptions WHERE user_id = ?", (uid,))
    assert (await cur.fetchone())[0] == 0

    # Orphaned assignment for q_iphone_pub must be deactivated or deleted
    cur_asg = await conn.execute("SELECT active FROM assignments WHERE query_id = 'q_iphone_pub'")
    row = await cur_asg.fetchone()
    assert row is None or row[0] == 0
    await conn.close()

