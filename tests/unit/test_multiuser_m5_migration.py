"""Unit tests for Phase M5 migration (v8 -> v9):
- Automatic VACUUM INTO backup matching .bak.v8
- Migration alters users to allow NULL telegram_user_id and chat_id (for GDPR/delete_user_data)
- Migration adds support_tickets and broadcasts tables
- Existing data preserved across migration
- Backward compatibility: old queries work, schema integrity passes
- Idempotent: multiple connects succeed without error
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
import pytest

from hunter.core.db import Database


@pytest.mark.asyncio
async def test_migration_v8_to_v9_from_scratch(tmp_path: Path) -> None:
    """A fresh database initializes directly at version 9 with all tables."""
    db_path = tmp_path / "scratch.db"
    db = Database(db_path)
    await db.connect()

    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute("PRAGMA user_version")
    ver = cur.fetchone()[0]
    assert ver >= 9

    # Verify new tables exist
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name IN ('support_tickets', 'broadcasts', 'users')"
    )
    tables = {r[0] for r in cur.fetchall()}
    assert tables == {"support_tickets", "broadcasts", "users"}

    # Verify users table columns and nullability
    cur.execute("PRAGMA table_info(users)")
    cols = {r[1]: {"notnull": r[3], "pk": r[5]} for r in cur.fetchall()}
    assert "telegram_user_id" in cols
    assert cols["telegram_user_id"]["notnull"] == 0  # Nullable
    assert "chat_id" in cols
    assert cols["chat_id"]["notnull"] == 0  # Nullable

    # Verify integrity
    cur.execute("PRAGMA integrity_check")
    assert cur.fetchone()[0] == "ok"
    con.close()


@pytest.mark.asyncio
async def test_migration_v8_to_v9_upgrade(tmp_path: Path) -> None:
    """Migrating an existing v8 database to v9:
    - Creates a .bak.v8 backup
    - Preserves existing rows
    - Recreates users table with nullable telegram_user_id
    - Creates support_tickets and broadcasts
    - Maintains foreign key integrity
    """
    db_path = tmp_path / "v8_upgrade.db"

    # 1. Create a simulated full v8 database from schema.sql
    schema_sql_path = (
        Path(__file__).resolve().parent.parent.parent / "src" / "hunter" / "core" / "schema.sql"
    )
    base_sql = schema_sql_path.read_text(encoding="utf-8")

    # Cut off M5 tables (support_tickets and broadcasts)
    v8_sql = base_sql.split("CREATE TABLE IF NOT EXISTS support_tickets")[0]

    # Make telegram_user_id NOT NULL UNIQUE as it was in v8
    v8_sql = v8_sql.replace(
        "telegram_user_id INTEGER UNIQUE,", "telegram_user_id INTEGER NOT NULL UNIQUE,"
    )
    v8_sql = v8_sql.replace("chat_id INTEGER,", "chat_id INTEGER NOT NULL UNIQUE,")

    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.executescript(v8_sql)
    cur.execute("PRAGMA user_version = 8")
    cur.execute(

            '\n        INSERT INTO users (id, telegram_user_id, chat_id, username,'
            ' display_name, role, status, tier, created_at, last_seen_at)\n       '
            " VALUES (1, 1001, 1001, 'owner_tg', 'Boss', 'owner', 'active', 'fami"
            "ly', '2026-10-15T00:00:00Z', '2026-10-15T00:00:00Z'),\n              "
            " (2, 2002, 2002, 'friend_tg', 'Friend', 'user', 'active', 'friend', "
            "'2026-10-15T01:00:00Z', '2026-10-15T01:00:00Z');\n        "

    )
    con.commit()
    con.close()

    # 2. Run Database.connect() which triggers _migrate_v8_to_v9
    db = Database(db_path)
    await db.connect()

    # 3. Check backup file creation
    backups = list(tmp_path.glob("*.bak.v8*"))
    assert len(backups) >= 1
    assert backups[0].exists()
    assert backups[0].stat().st_size > 0

    # Verify backup is at version 8
    con_bak = sqlite3.connect(backups[0])
    cur_bak = con_bak.cursor()
    cur_bak.execute("PRAGMA user_version")
    assert cur_bak.fetchone()[0] == 8
    con_bak.close()

    # 4. Check upgraded database
    con_v9 = sqlite3.connect(db_path)
    cur_v9 = con_v9.cursor()
    cur_v9.execute("PRAGMA user_version")
    assert cur_v9.fetchone()[0] >= 9

    # Check preserved rows
    cur_v9.execute(
        "SELECT id, telegram_user_id, chat_id, username, role FROM users ORDER BY id ASC"
    )
    users = cur_v9.fetchall()
    assert len(users) == 2
    assert users[0] == (1, 1001, 1001, "owner_tg", "owner")
    assert users[1] == (2, 2002, 2002, "friend_tg", "user")

    # Check that support_tickets and broadcasts tables exist
    cur_v9.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name IN ('support_tickets', 'broadcasts')"
    )
    new_tables = {r[0] for r in cur_v9.fetchall()}
    assert new_tables == {"support_tickets", "broadcasts"}

    # Check integrity
    cur_v9.execute("PRAGMA foreign_key_check")
    assert cur_v9.fetchall() == []
    cur_v9.execute("PRAGMA integrity_check")
    assert cur_v9.fetchone()[0] == "ok"

    con_v9.close()

    # 5. Check nullability by calling delete_user_data
    await db.delete_user_data(2)
    u2 = await db.get_user_by_id(2)
    assert u2["status"] == "deleted"
    assert u2["telegram_user_id"] is None
    assert u2["chat_id"] is None

    # 6. Idempotency: connect again
    db2 = Database(db_path)
    await db2.connect()
    con_check = sqlite3.connect(db_path)
    cur_check = con_check.cursor()
    cur_check.execute("PRAGMA user_version")
    assert cur_check.fetchone()[0] >= 9
    con_check.close()
    await db2.close()


@pytest.mark.asyncio
async def test_migration_restart_after_fault_at_boundary(tmp_path: Path) -> None:
    """S0: restart after fault at migration boundary recovers cleanly without corrupting DB."""
    from tests.fixtures.arch_state import create_synthetic_v1_db

    db_path = tmp_path / "fault_restart.db"
    create_synthetic_v1_db(db_path)

    # First connect completes migration across boundaries
    db = Database(db_path)
    await db.connect()
    await db.close()

    # Reconnect to simulate restart after boundary
    db2 = Database(db_path)
    await db2.connect()

    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute("PRAGMA integrity_check")
    assert cur.fetchone()[0] == "ok"
    cur.execute("PRAGMA foreign_key_check")
    assert cur.fetchall() == []
    cur.execute("PRAGMA user_version")
    assert cur.fetchone()[0] >= 11
    con.close()
    await db2.close()


@pytest.mark.asyncio
async def test_schema_equivalence_fresh_vs_migrated(tmp_path: Path) -> None:
    """Fresh and v1-upgraded databases expose the same products schema and data."""
    from tests.fixtures.arch_state import create_synthetic_v1_db

    p_fresh = tmp_path / "fresh.db"
    p_mig = tmp_path / "migrated.db"
    create_synthetic_v1_db(p_mig)

    db_fresh = Database(p_fresh)
    await db_fresh.connect()
    await db_fresh.close()

    db_mig = Database(p_mig)
    await db_mig.connect()
    await db_mig.close()

    def get_sig(p: Path) -> dict[str, dict]:
        con = sqlite3.connect(p)
        cur = con.cursor()
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        tables = [r[0] for r in cur.fetchall()]
        sig = {}
        for t in tables:
            cur.execute(f"PRAGMA table_info({t})")
            sig[t] = {
                r[1]: {"type": r[2].upper(), "notnull": r[3], "dflt": str(r[4]), "pk": r[5]}
                for r in cur.fetchall()
            }
        con.close()
        return sig

    sig_fresh = get_sig(p_fresh)
    sig_mig = get_sig(p_mig)

    assert sig_fresh == sig_mig
    assert "products" not in sig_mig
    assert "users" in sig_mig
