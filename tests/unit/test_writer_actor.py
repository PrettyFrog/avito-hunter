"""Unit tests for SQLiteWriterActor (T-463)."""

import asyncio
from pathlib import Path
import pytest
import aiosqlite

from hunter.storage.connection import StorageConfig, StorageConnectionManager
from hunter.storage.writer import (
    QueueFullError,
    SQLiteWriterActor,
    WriterStoppedError,
    _Envelope,
)


@pytest.fixture
async def setup_writer(tmp_path: Path):
    db_path = tmp_path / "writer_test.db"
    mgr = StorageConnectionManager(StorageConfig(db_path=db_path))
    writer = SQLiteWriterActor(mgr, max_queue_size=5)
    await writer.start()
    await writer.submit(
        lambda c: c.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, val TEXT)")
    )
    yield writer, mgr, db_path
    await writer.stop()
    await mgr.drain_and_close()


@pytest.mark.asyncio
async def test_writer_executes_and_commits(setup_writer) -> None:
    writer, mgr, _ = setup_writer
    row_id = await writer.submit(lambda c: c.execute("INSERT INTO items (val) VALUES ('test_val')"))
    reader = await mgr.open_read_connection()
    cur = await reader.execute("SELECT val FROM items WHERE id = 1")
    assert (await cur.fetchone())[0] == "test_val"
    await mgr.close_reader(reader)


@pytest.mark.asyncio
async def test_writer_barrier_guarantees_completion(setup_writer) -> None:
    writer, mgr, _ = setup_writer
    for i in range(5):
        await writer.submit(
            lambda c, v=f"v_{i}": c.execute(f"INSERT INTO items (val) VALUES ('{v}')")
        )
    assert await writer.barrier() > 0
    reader = await mgr.open_read_connection()
    cur = await reader.execute("SELECT count(*) FROM items")
    assert (await cur.fetchone())[0] == 5
    await mgr.close_reader(reader)


@pytest.mark.asyncio
async def test_writer_queue_full_raises_error(tmp_path: Path) -> None:
    mgr = StorageConnectionManager(StorageConfig(db_path=tmp_path / "qfull.db"))
    writer = SQLiteWriterActor(mgr, max_queue_size=1)
    writer._running = True
    fut = asyncio.get_running_loop().create_future()
    await writer._queue.put(_Envelope(lambda c: None, fut, 1))
    with pytest.raises(QueueFullError):
        await writer.submit(lambda c: None)


@pytest.mark.asyncio
async def test_writer_exception_rolls_back_cleanly(setup_writer) -> None:
    writer, mgr, _ = setup_writer

    async def _fail(c: aiosqlite.Connection) -> None:
        await c.execute("INSERT INTO items (val) VALUES ('fail')")
        raise ValueError("Fault")

    with pytest.raises(ValueError, match="Fault"):
        await writer.submit(_fail)

    await writer.submit(lambda c: c.execute("INSERT INTO items (val) VALUES ('ok')"))
    reader = await mgr.open_read_connection()
    cur = await reader.execute("SELECT val FROM items")
    assert [r[0] for r in await cur.fetchall()] == ["ok"]
    await mgr.close_reader(reader)


@pytest.mark.asyncio
async def test_writer_restart_lifecycle(tmp_path: Path) -> None:
    mgr = StorageConnectionManager(StorageConfig(db_path=tmp_path / "restart.db"))
    writer = SQLiteWriterActor(mgr, max_queue_size=5)
    await writer.start()
    await writer.submit(lambda c: c.execute("CREATE TABLE t (id INTEGER)"))
    await writer.stop()

    with pytest.raises(WriterStoppedError):
        await writer.submit(lambda c: c.execute("INSERT INTO t VALUES (1)"))

    await writer.start()
    await writer.submit(lambda c: c.execute("INSERT INTO t VALUES (1)"))
    await writer.stop()
    await mgr.drain_and_close()
