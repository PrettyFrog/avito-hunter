import asyncio

import pytest

from hunter.storage.connection import StorageConfig, StorageConnectionManager, StorageError


@pytest.mark.asyncio
async def test_writer_open_after_shutdown_is_rejected(tmp_path):
    mgr = StorageConnectionManager(StorageConfig(db_path=tmp_path / "db.sqlite"))
    await mgr.drain_and_close()
    with pytest.raises(StorageError):
        await mgr.open_write_connection()
    assert not (tmp_path / "db.sqlite").exists()


@pytest.mark.asyncio
async def test_writer_setup_failure_closes_connection(tmp_path, monkeypatch):
    mgr = StorageConnectionManager(StorageConfig(db_path=tmp_path / "db.sqlite"))

    class FakeConnection:
        row_factory = None
        closed = False

        async def execute(self, _sql):
            raise OSError("pragma fault")

        async def close(self):
            self.closed = True

    conn = FakeConnection()

    async def connect(_path):
        return conn

    monkeypatch.setattr("hunter.storage.connection.aiosqlite.connect", connect)
    with pytest.raises(OSError, match="pragma fault"):
        await mgr.open_write_connection()
    assert conn.closed
    assert mgr._writer is None


@pytest.mark.asyncio
async def test_writer_open_during_shutdown_is_rejected_and_closed(tmp_path, monkeypatch):
    mgr = StorageConnectionManager(StorageConfig(db_path=tmp_path / "db.sqlite"))
    started, release = asyncio.Event(), asyncio.Event()

    class FakeConnection:
        row_factory = None
        closed = False

        async def execute(self, _sql):
            return None

        async def close(self):
            self.closed = True

    conn = FakeConnection()

    async def connect(_path):
        started.set()
        await release.wait()
        return conn

    monkeypatch.setattr("hunter.storage.connection.aiosqlite.connect", connect)
    opening = asyncio.create_task(mgr.open_write_connection())
    await started.wait()
    closing = asyncio.create_task(mgr.drain_and_close())
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(StorageError):
        await opening
    await closing
    assert conn.closed
    assert mgr._writer is None
