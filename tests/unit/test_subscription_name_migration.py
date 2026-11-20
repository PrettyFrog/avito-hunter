"""Migration of per-user search names preserves existing subscriptions."""
import sqlite3

import pytest

from hunter.storage.migration_runner import MigrationRunner
from hunter.storage.migrations import v020_subscription_names


def old_database(path):
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE subscriptions (id TEXT PRIMARY KEY, user_id INTEGER)")
        conn.execute("INSERT INTO subscriptions VALUES ('sub_old', 10)")
        conn.execute("PRAGMA user_version = 19")


@pytest.mark.asyncio
async def test_upgrade_preserves_rows_creates_backup_and_can_retry(tmp_path):
    path = tmp_path / "old.db"
    old_database(path)
    with sqlite3.connect(path) as conn:
        runner = MigrationRunner(path)
        assert await runner.run_migrations(conn) == 22
        assert conn.execute("SELECT id, user_id, name, manual_keywords FROM subscriptions").fetchone() == (
            "sub_old", 10, "", ""
        )
        assert await runner.run_migrations(conn) == 22
    with sqlite3.connect(tmp_path / "old.db.bak.v19") as backup:
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 19
        assert backup.execute("SELECT id, user_id FROM subscriptions").fetchone() == ("sub_old", 10)


@pytest.mark.asyncio
async def test_interrupted_upgrade_rolls_back_and_recovers(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    old_database(path)
    original = v020_subscription_names._run
    async def fail_at_version(conn, sql):
        if sql == "PRAGMA user_version = 20":
            raise OSError("injected migration fault")
        return await original(conn, sql)
    monkeypatch.setattr(v020_subscription_names, "_run", fail_at_version)
    with sqlite3.connect(path) as conn:
        with pytest.raises(OSError, match="injected migration fault"):
            await MigrationRunner(path).run_migrations(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 19
        assert [r[1] for r in conn.execute("PRAGMA table_info(subscriptions)")] == ["id", "user_id"]
        assert conn.execute("SELECT id FROM subscriptions").fetchone() == ("sub_old",)
        monkeypatch.setattr(v020_subscription_names, "_run", original)
        assert await MigrationRunner(path).run_migrations(conn) == 22
