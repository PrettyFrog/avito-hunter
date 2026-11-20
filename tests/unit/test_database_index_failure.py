import aiosqlite
import pytest

from hunter.core.db import Database


@pytest.mark.asyncio
async def test_index_creation_failure_never_returns_usable_connection(tmp_path, monkeypatch):
    path = tmp_path / "database.sqlite"
    initial = Database(path)
    await initial.connect()
    await initial.close()
    original = aiosqlite.Connection.execute

    def fail_index(conn, sql, *args, **kwargs):
        if "CREATE INDEX IF NOT EXISTS idx_listing_events_ts" in sql:
            raise aiosqlite.OperationalError("injected index fault")
        return original(conn, sql, *args, **kwargs)

    monkeypatch.setattr(aiosqlite.Connection, "execute", fail_index)
    db = Database(path)
    for _ in range(2):
        with pytest.raises(aiosqlite.OperationalError, match="injected index fault"):
            await db.connect()
        assert db._conn is None


@pytest.mark.asyncio
async def test_migration_failure_discards_connection_before_retry(tmp_path, monkeypatch):
    db = Database(tmp_path / "new.sqlite")
    original_migrate = db.migrate
    calls = 0

    async def fail_once():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("migration fault")
        await original_migrate()

    monkeypatch.setattr(db, "migrate", fail_once)
    with pytest.raises(RuntimeError, match="migration fault"):
        await db.connect()
    assert db._conn is None
    conn = await db.connect()
    assert calls == 2
    assert (await (await conn.execute("PRAGMA integrity_check")).fetchone())[0] == "ok"
    await db.close()
