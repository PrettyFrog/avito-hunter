"""Regression test for F-089: Deleted/inactive user deliveries stuck in infinite DEFER loop.

Verifies that:
1. check_delivery_eligibility returns DeliveryEligibility.SUPPRESS when user status != 'active'.
2. DeliveryDispatcher suppresses pending deliveries for deleted users without endless retries.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
import pytest

from hunter.application.delivery_eligibility import DeliveryEligibility, check_delivery_eligibility
from hunter.storage.deliveries import ClaimedDelivery


class DummyConnMgr:
    def __init__(self, db_path: Path):
        self.db_path = str(db_path)

    async def open_read_connection(self):
        import aiosqlite
        return await aiosqlite.connect(self.db_path)

    async def close_reader(self, conn):
        await conn.close()


@pytest.mark.asyncio
async def test_repro_f089_delivery_eligibility_suppresses_deleted_user(tmp_path: Path):
    db_file = tmp_path / "test_hunter.db"
    con = sqlite3.connect(db_file)
    con.executescript("""
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            status TEXT NOT NULL
        );
        CREATE TABLE events (
            id TEXT PRIMARY KEY,
            avito_id INTEGER NOT NULL,
            new_price INTEGER NOT NULL,
            observed_at TEXT NOT NULL
        );
        CREATE TABLE recipient_events (
            event_id TEXT NOT NULL,
            user_id INTEGER NOT NULL
        );
        CREATE TABLE queries (
            id TEXT PRIMARY KEY,
            bootstrap_state TEXT NOT NULL
        );
        CREATE TABLE subscriptions (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            query_id TEXT NOT NULL,
            max_price INTEGER NOT NULL,
            manual_stopwords TEXT,
            manual_keywords TEXT DEFAULT '',
            enabled INTEGER NOT NULL
        );
        CREATE TABLE query_membership (
            query_id TEXT NOT NULL,
            avito_id INTEGER NOT NULL,
            last_seen TEXT NOT NULL
        );

        -- User 2 is deleted
        INSERT INTO users (id, status) VALUES (2, 'deleted');
        INSERT INTO events (id, avito_id, new_price, observed_at) VALUES ('evt_1', 12345, 1000, '2026-11-10T00:00:00+00:00');
        INSERT INTO recipient_events (event_id, user_id) VALUES ('evt_1', 2);
        INSERT INTO queries (id, bootstrap_state) VALUES ('q_1', 'ready');
        INSERT INTO subscriptions (id, user_id, query_id, max_price, manual_stopwords, enabled)
            VALUES ('sub_1', 2, 'q_1', 2000, '', 1);
        -- Unconfirmed in query membership so old code returned DEFER
        INSERT INTO query_membership (query_id, avito_id, last_seen)
            VALUES ('q_1', 12345, '2026-11-09T00:00:00+00:00');
    """)
    con.commit()
    con.close()

    delivery = ClaimedDelivery(
        id="del_1",
        event_id="evt_1",
        user_id=2,
        channel="telegram",
        destination_version=1,
        payload_version=1,
        payload={
            "avito_id": 12345,
            "new_price": 1000,
            "title": "Item",
            "url": "https://avito.ru/12345",
        },
        attempt=1,
        lease_token="lease_1",
        lease_until="2026-11-10T01:00:00+00:00",
    )

    mgr = DummyConnMgr(db_file)
    result = await check_delivery_eligibility(mgr, delivery)

    # Bug: returned DeliveryEligibility.DEFER because user active status was not checked.
    # Expected: must return DeliveryEligibility.SUPPRESS because user 2 is deleted.
    assert result == DeliveryEligibility.SUPPRESS


@pytest.mark.asyncio
async def test_repro_f089_dispatcher_suppresses_deleted_user_delivery(tmp_path: Path):
    from unittest.mock import AsyncMock
    from hunter.application.dispatcher import DeliveryDispatcher, SendResult
    from hunter.storage.connection import StorageConfig, StorageConnectionManager
    from hunter.storage.deliveries import DeliveryRepository
    from hunter.storage.writer import SQLiteWriterActor

    schema_file = Path(__file__).resolve().parent.parent.parent / "src/hunter/core/schema.sql"
    db_file = tmp_path / "hunter.db"
    con = sqlite3.connect(db_file)
    con.executescript(schema_file.read_text(encoding="utf-8"))
    con.executescript("""
        INSERT INTO users (id, status, role, created_at) VALUES (2, 'deleted', 'user', datetime('now'));
        INSERT INTO queries (id, canonical_url, bootstrap_state) VALUES ('q_1', 'https://avito.ru/q', 'ready');
        INSERT INTO subscriptions (id, user_id, query_id, max_price, name, enabled) VALUES ('sub_1', 2, 'q_1', 2000, 'iPhone', 1);
        INSERT INTO events (id, avito_id, revision, kind, observed_at, new_price, origin) VALUES ('evt_1', 12345, 1, 'NEW_LISTING', '2026-11-10T00:00:00+00:00', 1000, 'q_1');
        INSERT INTO recipient_events (event_id, user_id) VALUES ('evt_1', 2);
        INSERT INTO deliveries (id, event_id, user_id, channel, destination_version, payload_version, payload, status, available_at, attempt)
            VALUES ('del_1', 'evt_1', 2, 'telegram', 1, 1, '{"avito_id": 12345, "new_price": 1000, "title": "Item", "url": "https://avito.ru/12345"}', 'pending', datetime('now', '-1 minute'), 1);
    """)
    con.commit()
    con.close()

    mgr = StorageConnectionManager(StorageConfig(db_path=db_file))
    writer = SQLiteWriterActor(mgr)
    await writer.start()
    repo = DeliveryRepository(mgr, writer)

    fake_sender = AsyncMock(return_value=SendResult("sent", message_id="1"))
    dispatcher = DeliveryDispatcher(repo, {"telegram": fake_sender}, revalidate_deliveries=True)

    await dispatcher.dispatch_once()

    await writer.stop()
    await mgr.drain_and_close()

    # Verify: fake_sender was NOT called
    assert fake_sender.call_count == 0

    # Verify: delivery was suppressed, not pending
    con = sqlite3.connect(db_file)
    cur = con.execute("SELECT status FROM deliveries WHERE id = 'del_1'")
    assert cur.fetchone()[0] == "suppressed"
    con.close()

