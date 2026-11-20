import asyncio

import aiosqlite
import pytest

from hunter.storage.connection import StorageConfig, StorageConnectionManager
from hunter.storage.writer import SQLiteWriterActor, WriterError, WriterStoppedError


@pytest.mark.asyncio
async def test_failed_rollback_stops_writer_and_fails_queued_commands(tmp_path, monkeypatch):
    mgr = StorageConnectionManager(StorageConfig(db_path=tmp_path / "writer.sqlite"))
    writer = SQLiteWriterActor(mgr, max_queue_size=1)
    await writer.start()
    await writer.submit(lambda c: c.execute("CREATE TABLE items (id INTEGER)"))
    started, proceed = asyncio.Event(), asyncio.Event()
    original = aiosqlite.Connection.execute

    def fail_rollback(conn, sql, *args, **kwargs):
        if sql == "ROLLBACK":
            raise aiosqlite.OperationalError("injected rollback fault")
        return original(conn, sql, *args, **kwargs)

    monkeypatch.setattr(aiosqlite.Connection, "execute", fail_rollback)

    async def fail_after_insert(conn):
        await conn.execute("INSERT INTO items VALUES (1)")
        started.set()
        await proceed.wait()
        raise ValueError("injected command fault")

    first = asyncio.create_task(writer.submit(fail_after_insert))
    await started.wait()
    second = asyncio.create_task(writer.submit(lambda c: c.execute("INSERT INTO items VALUES (2)")))
    await asyncio.sleep(0)
    third = asyncio.create_task(
        writer.submit(lambda c: c.execute("INSERT INTO items VALUES (3)"), timeout=2)
    )
    await asyncio.sleep(0)
    proceed.set()
    try:
        with pytest.raises(WriterError):
            await asyncio.wait_for(first, timeout=2)
        with pytest.raises(WriterStoppedError):
            await asyncio.wait_for(second, timeout=2)
        with pytest.raises(WriterStoppedError):
            await asyncio.wait_for(third, timeout=2)
        with pytest.raises(WriterStoppedError):
            await writer.submit(lambda c: c.execute("INSERT INTO items VALUES (3)"))
    finally:
        await writer.stop()
        await mgr.drain_and_close()
    async with aiosqlite.connect(tmp_path / "writer.sqlite") as reader:
        assert (await (await reader.execute("SELECT count(*) FROM items")).fetchone())[0] == 0


@pytest.mark.asyncio
async def test_writer_connection_failure_fails_waiters_instead_of_hanging():
    class BrokenManager:
        async def open_write_connection(self):
            raise OSError("injected connection fault")

    writer = SQLiteWriterActor(BrokenManager())
    await writer.start()
    with pytest.raises(WriterStoppedError):
        await asyncio.wait_for(writer.submit(lambda conn: None), timeout=2)
    with pytest.raises(WriterStoppedError):
        await writer.barrier()
    await writer.stop()


@pytest.mark.asyncio
async def test_supervisor_detects_writer_exit():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from hunter.bootstrap import Bootstrap
    from hunter.core.config import Settings

    bootstrap = Bootstrap(Settings(env="test"))
    bootstrap.startup = AsyncMock()
    bootstrap.shutdown = AsyncMock()
    bootstrap.writer = SimpleNamespace(_worker_task=asyncio.create_task(asyncio.sleep(0)))
    assert await asyncio.wait_for(bootstrap.run_supervised(asyncio.Event()), timeout=1) == 1


@pytest.mark.asyncio
async def test_failed_begin_does_not_poison_writer(tmp_path, monkeypatch):
    mgr = StorageConnectionManager(StorageConfig(db_path=tmp_path / "begin.sqlite"))
    writer = SQLiteWriterActor(mgr)
    await writer.start()
    await writer.submit(lambda c: c.execute("CREATE TABLE items (id INTEGER)"))
    original = aiosqlite.Connection.execute
    fail_once = True

    def fail_begin(conn, sql, *args, **kwargs):
        nonlocal fail_once
        if sql == "BEGIN IMMEDIATE" and fail_once:
            fail_once = False
            raise aiosqlite.OperationalError("injected busy")
        return original(conn, sql, *args, **kwargs)

    monkeypatch.setattr(aiosqlite.Connection, "execute", fail_begin)
    try:
        with pytest.raises(aiosqlite.OperationalError, match="injected busy"):
            await writer.submit(lambda c: c.execute("INSERT INTO items VALUES (1)"))
        await writer.submit(lambda c: c.execute("INSERT INTO items VALUES (2)"))
    finally:
        await writer.stop()
        await mgr.drain_and_close()
