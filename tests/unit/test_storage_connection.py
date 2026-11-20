"""Unit tests for StorageConnectionManager lifecycle (T-462)."""

from pathlib import Path
import sqlite3
import pytest

from hunter.storage.connection import (
    StorageConfig,
    StorageConnectionManager,
    StorageError,
    classify_sqlite_error,
)


@pytest.mark.asyncio
async def test_write_and_read_pragmas(tmp_path: Path) -> None:
    db_path = tmp_path / "storage_test.db"
    config = StorageConfig(db_path=db_path, busy_timeout_ms=12000, synchronous="NORMAL")
    mgr = StorageConnectionManager(config)

    writer = await mgr.open_write_connection()
    await writer.execute("CREATE TABLE test_data (id INTEGER PRIMARY KEY, val TEXT)")
    await writer.execute("INSERT INTO test_data (id, val) VALUES (1, 'hello')")
    await writer.commit()

    # Verify write pragmas
    cur = await writer.execute("PRAGMA journal_mode")
    assert (await cur.fetchone())[0].lower() == "wal"
    cur = await writer.execute("PRAGMA foreign_keys")
    assert (await cur.fetchone())[0] == 1
    cur = await writer.execute("PRAGMA busy_timeout")
    assert (await cur.fetchone())[0] == 12000

    # Open read connection
    reader = await mgr.open_read_connection()
    cur = await reader.execute("PRAGMA query_only")
    assert (await cur.fetchone())[0] == 1

    # Reading succeeds
    cur = await reader.execute("SELECT val FROM test_data WHERE id = 1")
    assert (await cur.fetchone())[0] == "hello"

    # Writing on read-only connection fails
    with pytest.raises(sqlite3.OperationalError):
        await reader.execute("INSERT INTO test_data (id, val) VALUES (2, 'fail')")

    await mgr.close_reader(reader)
    await mgr.drain_and_close()


@pytest.mark.asyncio
async def test_drain_and_close_prevents_new_readers(tmp_path: Path) -> None:
    db_path = tmp_path / "drain_test.db"
    mgr = StorageConnectionManager(StorageConfig(db_path=db_path))

    writer = await mgr.open_write_connection()
    reader1 = await mgr.open_read_connection()
    reader2 = await mgr.open_read_connection()

    await mgr.drain_and_close()

    # Attempting to open reader after shutdown raises StorageError
    with pytest.raises(StorageError):
        await mgr.open_read_connection()


def test_classify_sqlite_error() -> None:
    assert classify_sqlite_error(sqlite3.OperationalError("database is locked")) == "busy"
    assert classify_sqlite_error(sqlite3.OperationalError("resource busy")) == "busy"
    assert (
        classify_sqlite_error(sqlite3.IntegrityError("UNIQUE constraint failed: test.id"))
        == "constraint"
    )
    assert (
        classify_sqlite_error(sqlite3.OperationalError("database disk image is malformed"))
        == "corrupt"
    )
    assert (
        classify_sqlite_error(sqlite3.OperationalError("no such table: unknown")) == "operational"
    )
    assert classify_sqlite_error(ValueError("other error")) == "other"
