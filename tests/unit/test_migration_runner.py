"""Unit tests for versioned migration runner (T-461)."""

from pathlib import Path
import sqlite3
import pytest

from hunter.core.db import Database
from hunter.storage.migration_runner import (
    FutureSchemaVersionError,
    MigrationChecksumError,
    MigrationRunner,
)


def test_backup_collision_preserves_old_backup_and_takes_current_snapshot(tmp_path: Path) -> None:
    db_path = tmp_path / "backup.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE sample (value TEXT)")
        conn.execute("INSERT INTO sample VALUES ('current')")
    old_backup = db_path.with_name("backup.db.bak.v18")
    old_backup.write_text("older backup", encoding="utf-8")

    MigrationRunner(db_path)._backup(18)

    candidates = list(tmp_path.glob("backup.db.bak.v18.*"))
    assert len(candidates) == 1
    assert old_backup.read_text(encoding="utf-8") == "older backup"
    with sqlite3.connect(candidates[0]) as conn:
        assert conn.execute("SELECT value FROM sample").fetchone() == ("current",)


@pytest.mark.asyncio
async def test_migration_runner_future_version_guard(tmp_path: Path) -> None:
    db_path = tmp_path / "future.db"
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA user_version = 99")
    con.commit()
    con.close()

    runner = MigrationRunner(db_path)
    con = sqlite3.connect(db_path)
    with pytest.raises(FutureSchemaVersionError):
        await runner.run_migrations(con)
    con.close()


@pytest.mark.asyncio
async def test_migration_runner_checksum_mismatch(tmp_path: Path) -> None:
    db_path = tmp_path / "chk.db"
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA user_version = 12")
    con.execute(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT, "
        "checksum TEXT, applied_at TEXT)"
    )
    # Corrupt checksum for v12
    con.execute(
        "INSERT INTO schema_migrations (version, name, checksum) "
        "VALUES (12, 'v012_subscriptions', 'bad_checksum_123')"
    )
    con.commit()
    con.close()

    runner = MigrationRunner(db_path)
    con = sqlite3.connect(db_path)
    with pytest.raises(MigrationChecksumError):
        await runner.run_migrations(con)
    con.close()


@pytest.mark.asyncio
async def test_migration_runner_forward_and_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "normal.db"
    db = Database(db_path)
    await db.connect()

    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute("PRAGMA user_version")
    assert cur.fetchone()[0] == 22

    # Check migration records
    cur.execute("SELECT version, name FROM schema_migrations ORDER BY version")
    rows = cur.fetchall()
    assert [r[0] for r in rows] == [12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22]

    # Re-run is idempotent
    runner = MigrationRunner(db_path)
    v = await runner.run_migrations(con)
    assert v == 22
    con.close()
    await db.close()
