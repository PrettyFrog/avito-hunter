import asyncio
from pathlib import Path

import pytest

from hunter.storage.connection import StorageConfig, StorageConnectionManager, StorageError


@pytest.mark.asyncio
async def test_reader_open_during_shutdown_closes_new_connection(tmp_path, monkeypatch):
    mgr = StorageConnectionManager(StorageConfig(db_path=tmp_path / "test.sqlite"))
    opened, release = asyncio.Event(), asyncio.Event()

    class FakeConnection:
        row_factory = None
        closed = False

        async def execute(self, _sql):
            return None

        async def close(self):
            self.closed = True

    connection = FakeConnection()

    async def connect(_path):
        opened.set()
        await release.wait()
        return connection

    monkeypatch.setattr("hunter.storage.connection.aiosqlite.connect", connect)
    opening = asyncio.create_task(mgr.open_read_connection())
    await opened.wait()
    await mgr.drain_and_close()
    release.set()
    with pytest.raises(StorageError):
        await opening
    assert connection.closed
    assert not mgr._readers


@pytest.mark.asyncio
async def test_reader_setup_failure_closes_connection(tmp_path: Path, monkeypatch):
    mgr = StorageConnectionManager(StorageConfig(db_path=tmp_path / "test.sqlite"))

    class FakeConnection:
        row_factory = None
        closed = False

        async def execute(self, _sql):
            raise OSError("pragma fault")

        async def close(self):
            self.closed = True

    connection = FakeConnection()

    async def connect(_path):
        return connection

    monkeypatch.setattr("hunter.storage.connection.aiosqlite.connect", connect)
    with pytest.raises(OSError, match="pragma fault"):
        await mgr.open_read_connection()
    assert connection.closed
    assert not mgr._readers


@pytest.mark.asyncio
async def test_cancelled_reader_setup_closes_connection(tmp_path, monkeypatch):
    mgr = StorageConnectionManager(StorageConfig(db_path=tmp_path / "test.sqlite"))
    started = asyncio.Event()

    class FakeConnection:
        row_factory = None
        closed = False

        async def execute(self, _sql):
            started.set()
            await asyncio.Event().wait()

        async def close(self):
            self.closed = True

    connection = FakeConnection()

    async def connect(_path):
        return connection

    monkeypatch.setattr("hunter.storage.connection.aiosqlite.connect", connect)
    opening = asyncio.create_task(mgr.open_read_connection())
    await started.wait()
    opening.cancel()
    with pytest.raises(asyncio.CancelledError):
        await opening
    assert connection.closed
    assert not mgr._readers
