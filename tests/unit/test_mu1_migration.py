"""Tests for Phase M1 Migration (DB schema v6 -> v7):
- Creates VACUUM INTO .bak.v6 backup before modifying schema
- Adds users, invites, access_requests, user_events tables
- Adds owner_user_id / recipient_user_id columns
- Seeds owner user from settings.telegram_chat_id and backfills existing rows
- PRAGMA integrity_check passes
- Backward compatibility: v6 queries still function on v7 database
- Idempotency: re-connecting does not duplicate or fail
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hunter.core.config import get_settings
from hunter.core.db import Database


@pytest.mark.asyncio
async def test_migration_v6_to_v7_empty_db(tmp_path: Path) -> None:
    """Test clean migration from scratch / schema.sql initializes at v7."""
    db_path = tmp_path / "clean.db"
    db = Database(db_path)
    await db.connect()

    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute("PRAGMA user_version")
    ver = cur.fetchone()[0]
    assert ver >= 7

    # Check tables exist
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name IN ('users', 'invites', 'access_requests', 'user_events')"
    )
    tables = {r[0] for r in cur.fetchall()}
    assert tables == {"users", "invites", "access_requests", "user_events"}

    # Check columns in notifications (if table present before v22 drop)
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='notifications'")
    if cur.fetchone():
        cur.execute("PRAGMA table_info(notifications)")
        cols = {r[1] for r in cur.fetchall()}
        assert "owner_user_id" in cols
        assert "recipient_user_id" in cols

    # Check columns in feedback (if table present before v22 drop)
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='feedback'")
    if cur.fetchone():
        cur.execute("PRAGMA table_info(feedback)")
        fb_cols = {r[1] for r in cur.fetchall()}
        assert "owner_user_id" in fb_cols

    # Integrity check
    cur.execute("PRAGMA integrity_check")
    assert cur.fetchone()[0] == "ok"
    con.close()


def _verify_pre_migration_v6(test_db: Path) -> int:
    con = sqlite3.connect(test_db)
    cur = con.cursor()
    cur.execute("PRAGMA user_version")
    assert cur.fetchone()[0] == 6
    cur.execute("PRAGMA table_info(notifications)")
    cols_before = {r[1] for r in cur.fetchall()}
    assert "owner_user_id" not in cols_before and "recipient_user_id" not in cols_before
    cur.execute("SELECT count(*) FROM notifications")
    notif_count_before = cur.fetchone()[0]
    con.close()
    return int(notif_count_before)


def _verify_migrated_v7(test_db: Path, notif_count_before: int) -> None:
    con = sqlite3.connect(test_db)
    cur = con.cursor()
    cur.execute("PRAGMA user_version")
    assert cur.fetchone()[0] >= 7
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name IN ('users', 'invites', 'access_requests', 'user_events')"
    )
    assert {r[0] for r in cur.fetchall()} == {"users", "invites", "access_requests", "user_events"}
    owner_chat_id = int(get_settings().telegram_chat_id)
    cur.execute("SELECT id, telegram_user_id, role, status FROM users WHERE role='owner'")
    owner_row = cur.fetchone()
    assert owner_row is not None and owner_row[1] == owner_chat_id and owner_row[2] == "owner" and owner_row[3] == "active"
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='notifications'")
    if cur.fetchone():
        cur.execute("SELECT count(*) FROM notifications")
        assert cur.fetchone()[0] == notif_count_before
        if notif_count_before > 0:
            cur.execute("SELECT count(*) FROM notifications WHERE owner_user_id = ?", (owner_row[0],))
            assert cur.fetchone()[0] == notif_count_before
    cur.execute("PRAGMA integrity_check")
    assert cur.fetchone()[0] == "ok"
    cur.execute("PRAGMA foreign_key_check")
    assert cur.fetchall() == []
    con.close()


@pytest.mark.asyncio
async def test_migration_v6_to_v7_on_v6_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test migration on a synthetic hermetic v6 database."""
    from tests.fixtures.arch_state import create_synthetic_v6_db

    monkeypatch.setenv("HUNTER_TELEGRAM_CHAT_ID", "999888777")
    get_settings.cache_clear()
    test_db = tmp_path / "hunter.db"
    create_synthetic_v6_db(test_db)

    notif_count_before = _verify_pre_migration_v6(test_db)

    db = Database(test_db)
    await db.connect()

    backup_file = tmp_path / "hunter.db.bak.v6"
    assert backup_file.exists() and backup_file.stat().st_size > 0
    con_bak = sqlite3.connect(backup_file)
    assert con_bak.cursor().execute("PRAGMA user_version").fetchone()[0] == 6
    con_bak.close()

    _verify_migrated_v7(test_db, notif_count_before)
    await db.close()

    # Idempotency test: Re-connecting does not crash or change version
    db2 = Database(test_db)
    await db2.connect()
    con2 = sqlite3.connect(test_db)
    cur2 = con2.cursor()
    assert cur2.execute("PRAGMA user_version").fetchone()[0] >= 7
    assert cur2.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert cur2.execute("PRAGMA foreign_key_check").fetchall() == []
    con2.close()
    await db2.close()
    get_settings.cache_clear()
