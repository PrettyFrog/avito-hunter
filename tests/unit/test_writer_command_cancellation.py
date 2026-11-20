import asyncio

import aiosqlite
import pytest

from hunter.storage.connection import StorageConfig, StorageConnectionManager
from hunter.storage.writer import SQLiteWriterActor, WriterStoppedError


@pytest.mark.asyncio
async def test_cancelled_command_fails_waiters_and_discards_transaction(tmp_path):
    path = tmp_path / "writer.sqlite"
    mgr = StorageConnectionManager(StorageConfig(db_path=path))
    writer = SQLiteWriterActor(mgr)
    await writer.start()
    await writer.submit(lambda c: c.execute("CREATE TABLE items (id INTEGER)"))
    started, release = asyncio.Event(), asyncio.Event()

    async def cancelled_command(conn):
        await conn.execute("INSERT INTO items VALUES (1)")
        started.set()
        await release.wait()
        raise asyncio.CancelledError

    first = asyncio.create_task(writer.submit(cancelled_command))
    await started.wait()
    second = asyncio.create_task(writer.submit(lambda c: c.execute("INSERT INTO items VALUES (2)")))
    await asyncio.sleep(0)
    release.set()
    try:
        results = await asyncio.wait_for(asyncio.gather(first, second, return_exceptions=True), 1)
        assert all(isinstance(result, WriterStoppedError) for result in results)
        assert not writer.is_running
    finally:
        await mgr.drain_and_close()
    async with aiosqlite.connect(path) as conn:
        assert (await (await conn.execute("SELECT count(*) FROM items")).fetchone())[0] == 0
