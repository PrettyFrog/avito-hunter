"""Unit tests for v14 slot and channel-destination migrations (T-466)."""

from pathlib import Path
import sqlite3
import pytest
import aiosqlite

from hunter.storage.migration_runner import LATEST_VERSION, MigrationRunner
from hunter.storage.migrations.v014_slots import apply_v014_migration

SCHEMA = Path(__file__).resolve().parent.parent.parent / "src/hunter/core/schema.sql"


@pytest.mark.asyncio
async def test_migration_chain_v11_includes_v14_slots(tmp_path: Path) -> None:
    db_path = tmp_path / "chain.db"
    con0 = sqlite3.connect(db_path)
    con0.executescript(
        SCHEMA.read_text(encoding="utf-8").split("CREATE TABLE IF NOT EXISTS queries")[0]
    )
    con0.execute("PRAGMA user_version = 11")
    con0.commit()
    con0.close()

    async with aiosqlite.connect(db_path) as con:
        assert await MigrationRunner(db_path).run_migrations(con) == LATEST_VERSION

    con_check = sqlite3.connect(db_path)
    cur = con_check.cursor()
    cur.execute("PRAGMA user_version")
    assert cur.fetchone()[0] == LATEST_VERSION
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name IN ('slots', 'assignments', 'session_versions', 'channel_destinations')"
    )
    assert {r[0] for r in cur.fetchall()} == {
        "slots",
        "assignments",
        "session_versions",
        "channel_destinations",
    }
    assert (
        (tmp_path / "chain.db.bak.v11").exists()
        and (tmp_path / "chain.db.bak.v12").exists()
        and (tmp_path / "chain.db.bak.v13").exists()
    )
    con_check.close()


@pytest.mark.asyncio
async def test_slot_assignment_generation_and_capacity_constraints(tmp_path: Path) -> None:
    db_path = tmp_path / "slot_constr.db"
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.execute("PRAGMA foreign_keys = ON")
    await apply_v014_migration(con, db_path)

    con.execute(
        "INSERT OR IGNORE INTO queries (id, canonical_url) VALUES ('q1', 'u1'), ('q2', 'u2')"
    )
    con.execute(
        "INSERT INTO slots (id, endpoint, cookie_namespace) "
        "VALUES ('slot_1', '127.0.0.1:8001', 'ns_1')",
    )
    con.execute(
        "INSERT INTO assignments (id, slot_id, query_id, position, generation) "
        "VALUES ('a0', 'slot_1', 'q1', 0, 1)",
    )

    # Position 4 rejected; duplicate position 0 rejected; duplicate generation 1 rejected
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO assignments (id, slot_id, query_id, position, generation) "
            "VALUES ('a_bad', 'slot_1', 'q2', 4, 2)",
        )
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO assignments (id, slot_id, query_id, position, generation) "
            "VALUES ('a_dup_pos', 'slot_1', 'q2', 0, 2)",
        )
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO assignments (id, slot_id, query_id, position, generation) "
            "VALUES ('a_dup_gen', 'slot_1', 'q2', 1, 1)",
        )

    con.execute(
        "INSERT INTO assignments (id, slot_id, query_id, position, generation) "
        "VALUES ('a1', 'slot_1', 'q2', 1, 2)",
    )
    con.close()


@pytest.mark.asyncio
async def test_verified_channel_destinations_no_fallback(tmp_path: Path) -> None:
    db_path = tmp_path / "dest_test.db"
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.execute("PRAGMA foreign_keys = ON")
    await apply_v014_migration(con, db_path)

    con.execute(
        "INSERT INTO users (id, role, status, created_at, last_seen_at) "
        "VALUES (101, 'user', 'active', 't', 't')"
    )
    con.execute(
        "INSERT INTO channel_destinations (user_id, channel, address, verified) "
        "VALUES (101, 'telegram', '101_chat', 1)"
    )

    # Invalid channel 'discord' rejected
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO channel_destinations (user_id, channel, address, verified) "
            "VALUES (101, 'discord', 'bad', 1)"
        )

    cur = con.cursor()
    cur.execute("SELECT address FROM channel_destinations WHERE user_id = 999 AND verified = 1")
    assert cur.fetchone() is None
    cur.execute(
        "SELECT address FROM channel_destinations WHERE user_id = 101 "
        "AND channel = 'telegram' AND verified = 1"
    )
    assert cur.fetchone()[0] == "101_chat"
    con.close()
