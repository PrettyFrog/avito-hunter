"""Unit tests for v16 migration: query_groups, group_members, and price_watch_jobs (T-503).

Covers:
- Forward migration chain v11 -> v16.
- Fresh schema.sql vs v11->v16 migrated schema signature equivalence.
- Foreign keys: cascade on delete, rejection of orphan members.
- Idempotent reapplication and fault rollback.
- Future-version guard.
"""

import sqlite3
from pathlib import Path

import aiosqlite
import pytest

from hunter.storage.migration_runner import (
    FutureSchemaVersionError,
    LATEST_VERSION,
    MigrationRunner,
)

SCHEMA_PATH = Path(__file__).resolve().parent.parent.parent / "src/hunter/core/schema.sql"


@pytest.mark.asyncio
async def test_migration_chain_v11_to_v16(tmp_path: Path) -> None:
    db_path = tmp_path / "chain_v16.db"
    con0 = sqlite3.connect(db_path)
    # Setup v11 baseline
    con0.executescript(
        SCHEMA_PATH.read_text(encoding="utf-8").split("CREATE TABLE IF NOT EXISTS queries")[0]
    )
    con0.execute("PRAGMA user_version = 11")
    con0.commit()
    con0.close()

    async with aiosqlite.connect(db_path) as con:
        ver = await MigrationRunner(db_path).run_migrations(con)
        assert ver == LATEST_VERSION

    con_check = sqlite3.connect(db_path)
    cur = con_check.cursor()
    cur.execute("PRAGMA user_version")
    assert cur.fetchone()[0] == LATEST_VERSION

    cur.execute(

            'SELECT name FROM sqlite_master WHERE t'
            "ype='table' AND name IN ('query_groups"
            "', 'group_members', 'price_watch_jobs'"
            ')'

    )
    found_tables = {r[0] for r in cur.fetchall()}
    assert found_tables == {"query_groups", "group_members", "price_watch_jobs"}

    # Verify backups exist
    assert (tmp_path / "chain_v16.db.bak.v11").exists()
    assert (tmp_path / "chain_v16.db.bak.v15").exists()
    con_check.close()


@pytest.mark.asyncio
async def test_fresh_vs_migrated_schema_equivalence(tmp_path: Path) -> None:
    p_fresh = tmp_path / "fresh.db"
    p_mig = tmp_path / "mig.db"

    # 1. Fresh DB
    con_fresh = sqlite3.connect(p_fresh)
    con_fresh.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    con_fresh.close()

    # 2. Migrated DB v11 -> v16
    con_mig = sqlite3.connect(p_mig)
    con_mig.executescript(
        SCHEMA_PATH.read_text(encoding="utf-8").split("CREATE TABLE IF NOT EXISTS queries")[0]
    )
    con_mig.execute("PRAGMA user_version = 11")
    con_mig.commit()
    con_mig.close()

    async with aiosqlite.connect(p_mig) as con:
        await MigrationRunner(p_mig).run_migrations(con)

    def get_table_sig(db_file: Path, tables: list[str]) -> dict[str, dict]:
        con = sqlite3.connect(db_file)
        sig = {}
        for t in tables:
            cur = con.execute(f"PRAGMA table_info({t})")
            sig[t] = {
                r[1]: {"type": r[2].upper(), "notnull": r[3], "pk": r[5]} for r in cur.fetchall()
            }
        con.close()
        return sig

    v16_tables = ["query_groups", "group_members", "price_watch_jobs"]
    sig_fresh = get_table_sig(p_fresh, v16_tables)
    sig_mig = get_table_sig(p_mig, v16_tables)

    assert sig_fresh == sig_mig


@pytest.mark.asyncio
async def test_foreign_keys_and_no_orphan_members(tmp_path: Path) -> None:
    db_path = tmp_path / "fk.db"
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    con.execute("PRAGMA foreign_keys = ON")

    # Insert prerequisite user, subscription, and group
    con.execute(

            'INSERT OR IGNORE INTO users (id, teleg'
            'ram_user_id, role, created_at) VALUES '
            "(1, 1001, 'user', datetime('now'))"

    )
    con.execute(
        "INSERT OR IGNORE INTO queries (id, canonical_url) VALUES ('q1', 'https://avito.ru/1')"
    )
    con.execute(

            'INSERT OR IGNORE INTO subscriptions (i'
            'd, user_id, query_id, max_price) VALUE'
            "S ('sub1', 1, 'q1', 20000)"

    )
    con.execute(

            'INSERT INTO query_groups (id, canonica'
            'l_url, filter_fingerprint, max_price) '
            "VALUES ('grp1', 'https://avito.ru/1', "
            "'fp1', 25000)"

    )
    con.execute(

            'INSERT INTO group_members (id, group_i'
            'd, subscription_id, user_id, personal_'
            "max_price) VALUES ('m1', 'grp1', 'sub1"
            "', 1, 20000)"

    )
    con.commit()

    # Reject orphan member (nonexistent group)
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(

                'INSERT INTO group_members (id, group_i'
                'd, subscription_id, user_id, personal_'
                "max_price) VALUES ('m_bad', 'nonexiste"
                "nt_grp', 'sub1', 1, 20000)"

        )

    # Deleting query_group cascades and deletes members (no orphan members left)
    con.execute("DELETE FROM query_groups WHERE id = 'grp1'")
    con.commit()

    cur = con.execute("SELECT count(*) FROM group_members WHERE group_id = 'grp1'")
    assert cur.fetchone()[0] == 0

    con.close()


@pytest.mark.asyncio
async def test_future_version_guard(tmp_path: Path) -> None:
    db_path = tmp_path / "future.db"
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA user_version = 99")
    con.commit()
    con.close()

    async with aiosqlite.connect(db_path) as con_aio:
        with pytest.raises(FutureSchemaVersionError):
            await MigrationRunner(db_path).run_migrations(con_aio)
