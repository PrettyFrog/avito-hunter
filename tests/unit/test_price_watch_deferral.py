"""HTTP admission refusal neither consumes retries nor creates price observations."""
from pathlib import Path
import sqlite3

import httpx
import pytest

from hunter.application.price_watch_worker import PriceWatchWorker
from hunter.storage.connection import StorageConfig, StorageConnectionManager
from hunter.storage.writer import SQLiteWriterActor
from hunter.transport.metered_http import HttpDeferred
from hunter.transport.price_fetch import PriceFetchAdapter


@pytest.fixture
async def writer(tmp_path):
    path = tmp_path / "deferred.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(Path("src/hunter/core/schema.sql").read_text())
        conn.execute("INSERT INTO price_watch_jobs(id,item_id,status,next_run_at,attempts,max_attempts) "
                     "VALUES('job','101','pending','2026-11-04T12:00:00+00:00',2,3)")
    manager = StorageConnectionManager(StorageConfig(db_path=path))
    actor = SQLiteWriterActor(manager)
    await actor.start()
    try:
        yield actor
    finally:
        await actor.stop()
        await manager.drain_and_close()


async def row(writer):
    async def read(conn):
        return tuple(await (await conn.execute(
            "SELECT status,attempts,next_run_at,lease_token FROM price_watch_jobs WHERE id='job'"
        )).fetchone())
    return await writer.submit(read)


@pytest.mark.asyncio
async def test_adapter_reports_deferral_without_sensitive_error_text():
    async def deferred(request):
        raise HttpDeferred("synthetic-secret", 130)
    async with httpx.AsyncClient(transport=httpx.MockTransport(deferred)) as client:
        adapter = PriceFetchAdapter(client, clock=lambda: 10)
        result = await adapter.fetch_card_price("https://www.avito.ru/item")
    assert result.status == "deferred"
    assert result.retry_after_sec == 120
    assert "secret" not in result.error


@pytest.mark.asyncio
async def test_deferral_preserves_retry_budget_and_durable_due_time(writer):
    calls = []
    async def deferred(request):
        calls.append(1)
        raise HttpDeferred("cooldown", 130)
    async with httpx.AsyncClient(transport=httpx.MockTransport(deferred)) as client:
        adapter = PriceFetchAdapter(client, clock=lambda: 10)
        worker = PriceWatchWorker(writer, fetcher=adapter, enabled=True, defer_sec=60)
        assert await worker.poll_once("2026-11-04T12:00:00+00:00") == 1
        assert await row(writer) == ("pending", 2, "2026-11-04T12:02:00+00:00", None)
        assert worker.requests_made == 0
        assert worker.jobs_deferred == 1
        assert worker.jobs_processed == 0
        assert await worker.poll_once("2026-11-04T12:01:59+00:00") == 0
        restarted = PriceWatchWorker(writer, fetcher=adapter, enabled=True)
        assert await restarted.poll_once("2026-11-04T12:01:59+00:00") == 0
        assert calls == [1]


@pytest.mark.asyncio
async def test_late_deferral_cannot_release_reassigned_lease(writer):
    async def deferred(request):
        async def reassign(conn):
            await conn.execute("UPDATE price_watch_jobs SET lease_token='new-owner',"
                               "next_run_at='2026-11-04T13:00:00+00:00' WHERE id='job'")
        await writer.submit(reassign)
        raise HttpDeferred("cooldown", 130)
    async with httpx.AsyncClient(transport=httpx.MockTransport(deferred)) as client:
        worker = PriceWatchWorker(writer, fetcher=PriceFetchAdapter(client, clock=lambda: 10), enabled=True)
        await worker.poll_once("2026-11-04T12:00:00+00:00")
    assert await row(writer) == ("leased", 2, "2026-11-04T13:00:00+00:00", "new-owner")
    assert worker.jobs_deferred == 0
