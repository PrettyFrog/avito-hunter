"""Unit tests for v12 migration: queries, subscriptions, and memberships (T-459)."""

import sqlite3
from pathlib import Path

import pytest

from hunter.core.db import Database


@pytest.mark.asyncio
async def test_fresh_database_initializes_at_v12(tmp_path: Path) -> None:
    db_path = tmp_path / "fresh_v12.db"
    db = Database(db_path)
    await db.connect()
    await db.close()

    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute("PRAGMA user_version")
    ver = cur.fetchone()[0]
    assert ver >= 12

    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
        "('queries', 'subscriptions', 'query_membership', 'subscription_seen')"
    )
    tables = {r[0] for r in cur.fetchall()}
    assert tables == {"queries", "subscriptions", "query_membership", "subscription_seen"}

    cur.execute("PRAGMA integrity_check")
    assert cur.fetchone()[0] == "ok"
    cur.execute("PRAGMA foreign_key_check")
    assert cur.fetchall() == []
    con.close()


@pytest.mark.asyncio
async def test_migration_v11_to_v12_with_backup_and_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy_v11.db"
    # Seed a standard v11 database
    schema_file = Path(__file__).resolve().parent.parent.parent / "src/hunter/core/schema.sql"
    con0 = sqlite3.connect(db_path)
    con0.executescript(schema_file.read_text(encoding="utf-8"))
    con0.execute("PRAGMA user_version = 11")
    con0.commit()
    con0.close()

    # Migrate v11 -> v12
    db = Database(db_path)
    await db.connect()
    await db.close()

    # Verify backup exists
    bak_path = tmp_path / "legacy_v11.db.bak.v11"
    assert bak_path.exists()
    con_bak = sqlite3.connect(bak_path)
    cur_bak = con_bak.cursor()
    cur_bak.execute("PRAGMA user_version")
    assert cur_bak.fetchone()[0] == 11
    con_bak.close()

    # Verify migrated state
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute("PRAGMA user_version")
    assert cur.fetchone()[0] >= 12
    con.close()

    # Restart / idempotent reconnect
    db_restart = Database(db_path)
    await db_restart.connect()
    await db_restart.close()


@pytest.mark.asyncio
async def test_tenant_subscription_isolation(tmp_path: Path) -> None:
    db_path = tmp_path / "tenant_iso.db"
    db = Database(db_path)
    conn = await db.connect()

    # Insert query
    await conn.execute(
        "INSERT INTO queries (id, canonical_url) VALUES ('q_mac', 'https://avito.ru/macbook')"
    )

    # Insert subscriptions for two distinct users on the same query
    await conn.execute(
        "INSERT INTO subscriptions (id, user_id, query_id, max_price, manual_stopwords) "
        "VALUES ('sub_user1', 101, 'q_mac', 100000, 'broken,parts')"
    )
    await conn.execute(
        "INSERT INTO subscriptions (id, user_id, query_id, max_price, manual_stopwords) "
        "VALUES ('sub_user2', 102, 'q_mac', 120000, 'locked')"
    )
    await conn.commit()

    # User 101 query
    cur1 = await conn.execute(
        "SELECT id, max_price, manual_stopwords FROM subscriptions WHERE user_id = 101"
    )
    rows1 = await cur1.fetchall()
    assert len(rows1) == 1
    assert rows1[0][0] == "sub_user1"
    assert rows1[0][1] == 100000

    # User 102 query
    cur2 = await conn.execute(
        "SELECT id, max_price, manual_stopwords FROM subscriptions WHERE user_id = 102"
    )
    rows2 = await cur2.fetchall()
    assert len(rows2) == 1
    assert rows2[0][0] == "sub_user2"
    assert rows2[0][1] == 120000

    # Foreign key checks
    cur_fk = await conn.execute("PRAGMA foreign_key_check")
    assert await cur_fk.fetchall() == []

    await db.close()
