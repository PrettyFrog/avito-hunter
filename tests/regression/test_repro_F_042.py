"""Regression test for F-042: Multi-user feedback isolation in Telegram router and store.

In a multi-user environment:
1. When multiple users receive alerts for the same avito_id, an earlier recipient
   must not be rejected with CALLBACK_NOT_RECIPIENT when rating their alert.
2. record_feedback with owner_user_id must update the specific recipient's notification row,
   rather than the latest recipient's row.
3. In Public v2 (deliveries table), non-recipients must be rejected with CALLBACK_NOT_RECIPIENT,
   and recipients must be accepted with their delivery updated.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

from aiogram.types import CallbackQuery
from aiogram.types import User as TgUser
import aiosqlite
import pytest

from hunter.core.db import Database
from hunter.core.models import User
from hunter.feedback.store import record_feedback
from hunter.notify.messages_ru import CALLBACK_NOT_RECIPIENT
from hunter.notify.telegram_router_parts.setup_telegram_router_on_alert_ref_add_208 import (
    handle_feedback_callback,
)


def _make_query(avito_id: str, code: str, user_id: int) -> tuple[CallbackQuery, AsyncMock]:
    tg_u = TgUser(id=user_id, is_bot=False, first_name=f"User{user_id}")
    query = CallbackQuery(
        id=f"cb_{user_id}",
        from_user=tg_u,
        chat_instance="ci",
        data=f"fb:{avito_id}:{code}",
    )
    answer_mock = AsyncMock()
    object.__setattr__(query, "answer", answer_mock)
    return query, answer_mock


@pytest.mark.asyncio
async def test_multiuser_feedback_isolation_earlier_recipient(initialized_db: Path) -> None:
    """User 101 (first recipient) must not be rejected when User 102 received same lot later."""
    db = Database(initialized_db)
    await db.connect()
    async with aiosqlite.connect(initialized_db) as conn:
        await conn.execute("CREATE TABLE IF NOT EXISTS notifications (id INTEGER PRIMARY KEY, avito_id TEXT, channel TEXT, recipient_user_id INTEGER, owner_user_id INTEGER, sent_at TEXT, feedback TEXT)")
        await conn.execute("INSERT INTO listings (avito_id, url, first_seen, last_seen, status) VALUES ('lot_multi', 'http://a', '2026-01-01', '2026-01-01', 'active')")
        await conn.execute("INSERT INTO users (id, telegram_user_id, chat_id, role, created_at) VALUES (101, 101, 101, 'user', '2026-01-01')")
        await conn.execute("INSERT INTO users (id, telegram_user_id, chat_id, role, created_at) VALUES (102, 102, 102, 'user', '2026-01-01')")
        await conn.execute(
            "INSERT INTO notifications (id, avito_id, channel, recipient_user_id, owner_user_id) "
            "VALUES (1, 'lot_multi', 'telegram', 101, 101), (2, 'lot_multi', 'telegram', 102, 102)"
        )
        await conn.commit()

    u101 = User(id=101, telegram_user_id=101, chat_id=101, role="user", created_at="2026-01-01")
    labels = {"good": "Хорошая цена"}

    query_a, answer_a = _make_query("lot_multi", "good", 101)
    await handle_feedback_callback(query_a, u101, db, labels)

    # In buggy code, answer_a was called with CALLBACK_NOT_RECIPIENT because row 2 (rec_id=102) was returned
    assert answer_a.call_count >= 1
    first_arg = answer_a.call_args[0][0]
    assert first_arg != CALLBACK_NOT_RECIPIENT, "Earlier recipient was wrongly rejected with CALLBACK_NOT_RECIPIENT"
    assert "Хорошая цена" in first_arg

    # Check notification rows: row 1 must be updated, row 2 must remain None
    async with aiosqlite.connect(initialized_db) as conn:
        conn.row_factory = aiosqlite.Row
        rows = await (await conn.execute("SELECT id, recipient_user_id, feedback FROM notifications WHERE avito_id = 'lot_multi' ORDER BY id")).fetchall()
        assert rows[0]["feedback"] == "good"
        assert rows[1]["feedback"] is None


@pytest.mark.asyncio
async def test_record_feedback_scopes_by_owner_user_id(initialized_db: Path) -> None:
    """record_feedback with owner_user_id must update only that user's notification row."""
    db = Database(initialized_db)
    await db.connect()
    async with aiosqlite.connect(initialized_db) as conn:
        await conn.execute("CREATE TABLE IF NOT EXISTS notifications (id INTEGER PRIMARY KEY, avito_id TEXT, channel TEXT, recipient_user_id INTEGER, owner_user_id INTEGER, sent_at TEXT, feedback TEXT)")
        await conn.execute("INSERT INTO listings (avito_id, url, first_seen, last_seen, status) VALUES ('lot_rf', 'http://a', '2026-01-01', '2026-01-01', 'active')")
        await conn.execute("INSERT INTO users (id, telegram_user_id, chat_id, role, created_at) VALUES (201, 201, 201, 'user', '2026-01-01')")
        await conn.execute("INSERT INTO users (id, telegram_user_id, chat_id, role, created_at) VALUES (202, 202, 202, 'user', '2026-01-01')")
        await conn.execute(
            "INSERT INTO notifications (id, avito_id, channel, recipient_user_id, owner_user_id) "
            "VALUES (10, 'lot_rf', 'telegram', 201, 201), (11, 'lot_rf', 'telegram', 202, 202)"
        )
        await conn.commit()

    await record_feedback(db, "lot_rf", "target_model", owner_user_id=201)

    async with aiosqlite.connect(initialized_db) as conn:
        conn.row_factory = aiosqlite.Row
        rows = await (await conn.execute("SELECT id, recipient_user_id, feedback FROM notifications WHERE avito_id = 'lot_rf' ORDER BY id")).fetchall()
        assert rows[0]["feedback"] == "target_model"
        assert rows[1]["feedback"] is None


@pytest.mark.asyncio
async def test_public_v2_deliveries_authorization_and_feedback(initialized_db: Path) -> None:
    """In Public v2, non-recipients must be rejected, and recipients accepted."""
    db = Database(initialized_db)
    async with aiosqlite.connect(initialized_db) as conn:
        await conn.execute("INSERT INTO listings (avito_id, url, first_seen, last_seen, status) VALUES ('lot_v2', 'http://a', '2026-01-01', '2026-01-01', 'active')")
        await conn.execute("INSERT INTO users (id, telegram_user_id, chat_id, role, created_at) VALUES (301, 301, 301, 'user', '2026-01-01')")
        await conn.execute("INSERT INTO users (id, telegram_user_id, chat_id, role, created_at) VALUES (302, 302, 302, 'user', '2026-01-01')")
        await conn.execute(
            "INSERT INTO events (id, avito_id, revision, kind, observed_at, origin) "
            "VALUES ('evt_v2', 'lot_v2', 1, 'NEW_LISTING', '2026-01-01T00:00:00Z', 'q1')"
        )
        await conn.execute(
            "INSERT INTO deliveries (id, event_id, user_id, channel, available_at, payload) "
            "VALUES ('del_301', 'evt_v2', 301, 'telegram', '2026-01-01T00:00:00Z', '{}')"
        )
        await conn.commit()

    u301 = User(id=301, telegram_user_id=301, chat_id=301, role="user", created_at="2026-01-01")
    u302 = User(id=302, telegram_user_id=302, chat_id=302, role="user", created_at="2026-01-01")
    labels = {"ok": "Всё верно"}

    # User 302 was NOT a recipient of lot_v2 delivery -> must be rejected
    query_302, answer_302 = _make_query("lot_v2", "ok", 302)
    await handle_feedback_callback(query_302, u302, db, labels)
    assert answer_302.call_count >= 1
    assert answer_302.call_args[0][0] == CALLBACK_NOT_RECIPIENT

    # User 301 WAS the recipient of lot_v2 delivery -> must be accepted
    query_301, answer_301 = _make_query("lot_v2", "ok", 301)
    await handle_feedback_callback(query_301, u301, db, labels)
    assert answer_301.call_count >= 1
    assert answer_301.call_args[0][0] != CALLBACK_NOT_RECIPIENT
    assert "Всё верно" in answer_301.call_args[0][0]

    # Verify delivery payload recorded the feedback
    async with aiosqlite.connect(initialized_db) as conn:
        conn.row_factory = aiosqlite.Row
        row = await (await conn.execute("SELECT payload FROM deliveries WHERE id = 'del_301'")).fetchone()
        payload = json.loads(row["payload"])
        assert payload.get("feedback") == "ok"
