"""Tests for public status and subscription commands v2 (S4 / T-511)."""

from __future__ import annotations

import re
from datetime import UTC, datetime

import aiosqlite
import pytest

from hunter.core.models import User
from hunter.notify.telegram_handlers.public_status import (
    delete_subscription,
    edit_subscription,
    get_public_status_text,
    list_public_subscriptions,
    pause_subscription,
    resume_subscription,
)
from hunter.storage.connection import StorageConnectionManager
from hunter.storage.subscriptions import SubscriptionActor, SubscriptionRepository
from hunter.storage.writer import SQLiteWriterActor


class DummyDb:
    def __init__(self, conn, repo):
        self._conn, self.subscription_repo, self.pending = conn, repo, {}

    async def connect(self):
        return self._conn

    async def get_pending_product(self, chat_id):
        return self.pending.get(chat_id)

    async def delete_pending_product(self, chat_id):
        self.pending.pop(chat_id, None)


@pytest.fixture
async def app_setup(tmp_path):
    conn = await aiosqlite.connect(tmp_path / "test.db")
    await conn.execute("PRAGMA foreign_keys = ON;")
    await conn.executescript("""
        CREATE TABLE users (id INTEGER PRIMARY KEY, role TEXT DEFAULT 'user');
        CREATE TABLE queries (id TEXT PRIMARY KEY, canonical_url TEXT UNIQUE,
            contract_version INTEGER DEFAULT 1, bootstrap_state TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE subscriptions (
            id TEXT PRIMARY KEY, user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            query_id TEXT REFERENCES queries(id) ON DELETE CASCADE, max_price INTEGER NOT NULL,
            manual_stopwords TEXT DEFAULT '', name TEXT NOT NULL DEFAULT '',
            manual_keywords TEXT NOT NULL DEFAULT '',
            enabled INTEGER DEFAULT 1, version INTEGER DEFAULT 1,
            ai_enrichment_enabled INTEGER DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(user_id, query_id));
        CREATE TABLE subscription_seen (
            subscription_id TEXT NOT NULL, avito_id TEXT NOT NULL, baseline_epoch INTEGER DEFAULT 0,
            last_revision INTEGER DEFAULT 1, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (subscription_id, avito_id));
        CREATE TABLE deliveries (
            id TEXT PRIMARY KEY, event_id TEXT, user_id INTEGER, channel TEXT,
            destination_version INTEGER, payload_version INTEGER, payload TEXT, status TEXT,
            available_at TEXT, attempt INTEGER, created_at TEXT);
        CREATE TABLE events (id TEXT PRIMARY KEY, origin TEXT);
        CREATE TABLE listing_state (avito_id TEXT PRIMARY KEY, revision INTEGER DEFAULT 1);
        CREATE TABLE query_membership (query_id TEXT, avito_id TEXT);
        CREATE TABLE subscription_tombstones (
            id TEXT PRIMARY KEY, user_id INTEGER, query_id TEXT,
            tombstoned_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE runtime (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE slots (
            id TEXT PRIMARY KEY, health_state TEXT NOT NULL DEFAULT 'healthy');
        CREATE TABLE assignments (
            id TEXT PRIMARY KEY, query_id TEXT NOT NULL, slot_id TEXT NOT NULL,
            position INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1,
            generation INTEGER NOT NULL DEFAULT 1, updated_at TEXT);
        CREATE TABLE group_members (subscription_id TEXT NOT NULL);
        INSERT INTO users (id) VALUES (1), (2);
    """)
    await conn.commit()

    class DirectMgr(StorageConnectionManager):
        def __init__(self):
            pass

        async def open_read_connection(self):
            return conn

        async def close_reader(self, c):
            pass

    class DirectWriter(SQLiteWriterActor):
        def __init__(self):
            pass

        async def submit(self, fn):
            return await fn(conn)

    repo = SubscriptionRepository(DirectMgr(), DirectWriter())
    db = DummyDb(conn, repo)
    yield conn, repo, db
    await conn.close()


@pytest.mark.asyncio
async def test_cadence_day_night_and_status_formatting(app_setup):
    conn, repo, db = app_setup
    u1 = User(
        id=1, telegram_user_id=1, chat_id=1, role="user", quiet_start=23, quiet_end=8, created_at=""
    )
    await repo.create(
        SubscriptionActor(1), "sub_1", 1, "q_1", 15000, "битый", "https://avito.ru/q1"
    )
    await conn.execute("UPDATE queries SET bootstrap_state = 'waiting' WHERE id = 'q_1'")
    await conn.commit()

    status_day = await get_public_status_text(
        u1, db, now_dt=datetime(2026, 10, 23, 11, 0, tzinfo=UTC)
    )
    assert (
        "30 с</b> (день)" in status_day
        and "sub_1" in status_day
        and "ожидание окна проверки" in status_day
    )
    assert "SERP (поиск)" in status_day and "Тихие часы: с 23:00 до 8:00" in status_day

    status_night = await get_public_status_text(
        u1, db, now_dt=datetime(2026, 10, 23, 1, 0, tzinfo=UTC)
    )
    assert "3 мин</b> (ночь)" in status_night

    assert not re.search(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", status_day)
    assert "cookie" not in status_day.lower() and "slot" not in status_day.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("now_dt", "expected"),
    [
        (datetime(2026, 10, 23, 4, 0, tzinfo=UTC), "30 с</b> (день)"),
        (datetime(2026, 10, 23, 21, 59, tzinfo=UTC), "30 с</b> (день)"),
        (datetime(2026, 10, 23, 22, 0, tzinfo=UTC), "3 мин</b> (ночь)"),
    ],
)
async def test_status_uses_authoritative_msk_boundaries(app_setup, now_dt, expected):
    _, repo, db = app_setup
    user = User(id=1, telegram_user_id=1, chat_id=1, role="user", created_at="")
    await repo.create(
        SubscriptionActor(1), "sub_boundary", 1, "q_boundary", 10000, "", "https://avito.ru/q"
    )
    status = await get_public_status_text(user, db, now_dt=now_dt)
    assert expected in status


@pytest.mark.asyncio
async def test_status_shows_last_success_and_degraded_without_slot_secrets(app_setup):
    conn, repo, db = app_setup
    user = User(id=1, telegram_user_id=1, chat_id=1, role="user", created_at="")
    await repo.create(
        SubscriptionActor(1), "sub_health", 1, "q_health", 10000, "", "https://avito.ru/q"
    )
    await conn.execute(
        "INSERT INTO slots (id, health_state) VALUES ('private-slot-42', 'quarantined')"
    )
    await conn.execute(
        "INSERT INTO assignments (id, query_id, slot_id, position, active) "
        "VALUES ('a_health', 'q_health', 'private-slot-42', 0, 1)"
    )
    await conn.execute(
        "INSERT INTO runtime (key, value) VALUES (?, ?)",
        ("query:last_successful_poll:q_health", "2026-10-23T04:00:00+00:00"),
    )
    await conn.commit()
    status = await get_public_status_text(
        user, db, now_dt=datetime(2026, 10, 23, 4, 1, tzinfo=UTC)
    )
    assert "Последняя успешная проверка: 23.10 07:00 МСК" in status
    assert "проверка замедлена" in status
    assert "private-slot-42" not in status


@pytest.mark.asyncio
async def test_scoped_isolation_and_no_replay_on_edit(app_setup):
    conn, repo, db = app_setup
    u1 = User(id=1, telegram_user_id=1, chat_id=1, role="user", created_at="")
    await repo.create(SubscriptionActor(1), "sub_u1", 1, "q_1", 10000, "", "https://avito.ru/q1")
    await repo.create(SubscriptionActor(2), "sub_u2", 2, "q_2", 20000, "", "https://avito.ru/q2")

    assert "sub_u1" in (await list_public_subscriptions(u1, db)) and "sub_u2" not in (
        await list_public_subscriptions(u1, db)
    )
    for fn in (pause_subscription, resume_subscription, delete_subscription):
        assert (await fn(u1, "sub_u2", db))[0] is False
    assert (await edit_subscription(u1, "sub_u2", 25000, "битый", db))[0] is False

    assert (await pause_subscription(u1, "sub_u1", db))[0] is True
    assert (await resume_subscription(u1, "sub_u1", db))[0] is True

    await conn.execute(
        "INSERT INTO subscription_seen (subscription_id, avito_id) VALUES ('sub_u1', 'lot_99')"
    )
    await conn.commit()

    ok, msg = await edit_subscription(u1, "sub_u1", 12000, "запчасти", db)
    assert ok is True and "12 000" in msg
    cur = await conn.execute(
        "SELECT avito_id FROM subscription_seen WHERE subscription_id = 'sub_u1'"
    )
    assert [r[0] for r in await cur.fetchall()] == ["lot_99"]

    assert (await delete_subscription(u1, "sub_u1", db))[0] is True
    assert await repo.is_tombstoned("sub_u1") is True


@pytest.mark.asyncio
async def test_user_sees_escaped_name_in_subscription_list(app_setup):
    _, repo, db = app_setup
    await repo.create(SubscriptionActor(1), 'mine', 1, 'q1', 10000, name='Mac <тест>')
    await repo.create(SubscriptionActor(2), 'theirs', 2, 'q2', 20000, name='Чужой')
    user = User(id=1, telegram_user_id=1, chat_id=1, role='user', created_at='')
    text = await list_public_subscriptions(user, db)
    assert 'Mac &lt;тест&gt;' in text
    assert 'Чужой' not in text

@pytest.mark.asyncio
async def test_owner_personal_list_does_not_include_other_users(app_setup):
    _, repo, db = app_setup
    await repo.create(SubscriptionActor(1), 'mine', 1, 'q1', 10000, name='Мой поиск')
    await repo.create(SubscriptionActor(2), 'theirs', 2, 'q2', 20000, name='Чужой')
    user = User(id=1, telegram_user_id=1, chat_id=1, role='owner', created_at='')
    text = await list_public_subscriptions(user, db)
    assert 'theirs' not in text

@pytest.mark.asyncio
async def test_status_displays_only_owners_named_searches(app_setup):
    _, repo, db = app_setup
    await repo.create(SubscriptionActor(1), 'mine', 1, 'q1', 10000, name='Свой')
    await repo.create(SubscriptionActor(2), 'theirs', 2, 'q2', 20000, name='Чужой')
    user = User(id=1, telegram_user_id=1, chat_id=1, role='owner', created_at='')
    text = await get_public_status_text(user, db)
    assert 'Свой' in text and 'theirs' not in text
