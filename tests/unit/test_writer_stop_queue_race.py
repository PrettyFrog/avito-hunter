import asyncio

import pytest

from hunter.storage.connection import StorageConfig, StorageConnectionManager
from hunter.storage.writer import SQLiteWriterActor, WriterStoppedError


@pytest.mark.asyncio
async def test_rejected_submit_during_stop_never_commits(tmp_path):
    mgr = StorageConnectionManager(StorageConfig(db_path=tmp_path / "db.sqlite"))
    writer = SQLiteWriterActor(mgr, max_queue_size=1)
    await writer.start()
    await writer.submit(lambda c: c.execute("CREATE TABLE items (id INTEGER)"))
    started, release = asyncio.Event(), asyncio.Event()

    async def first_command(conn):
        started.set()
        await release.wait()
        await conn.execute("INSERT INTO items VALUES (1)")

    first = asyncio.create_task(writer.submit(first_command))
    await started.wait()
    second = asyncio.create_task(writer.submit(lambda c: c.execute("INSERT INTO items VALUES (2)")))
    await asyncio.sleep(0)
    third = asyncio.create_task(
        writer.submit(lambda c: c.execute("INSERT INTO items VALUES (3)"), timeout=2)
    )
    await asyncio.sleep(0)
    stopper = asyncio.create_task(writer.stop())
    await asyncio.sleep(0)
    release.set()
    try:
        await asyncio.wait_for(first, 2)
        await asyncio.wait_for(second, 2)
        with pytest.raises(WriterStoppedError):
            await asyncio.wait_for(third, 2)
        await asyncio.wait_for(stopper, 2)
        reader = await mgr.open_read_connection()
        try:
            rows = await (await reader.execute("SELECT id FROM items ORDER BY id")).fetchall()
            assert [row[0] for row in rows] == [1, 2]
        finally:
            await mgr.close_reader(reader)
    finally:
        await writer.stop()
        await mgr.drain_and_close()
