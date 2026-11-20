"""Tests for price watch worker and card price adapter (S7 / T-508)."""

from __future__ import annotations

from pathlib import Path
import sqlite3
import pytest
import aiosqlite

from hunter.application.price_watch_worker import PriceWatchWorker
from hunter.storage.connection import StorageConfig, StorageConnectionManager
from hunter.storage.writer import SQLiteWriterActor
from hunter.transport.price_fetch import PriceFetchAdapter, PriceFetchResult

SCHEMA = Path(__file__).resolve().parent.parent.parent / "src/hunter/core/schema.sql"


class FakePriceFetcher(PriceFetchAdapter):
    def __init__(self, outcomes: list[PriceFetchResult] | None = None) -> None:
        super().__init__()
        self.outcomes = list(outcomes or [])

    async def fetch_card_price(self, url: str) -> PriceFetchResult:
        return (
            self.outcomes.pop(0)
            if self.outcomes
            else PriceFetchResult(status="unknown", error="empty")
        )


async def _run_sql(writer: SQLiteWriterActor, sql: str, params: tuple = ()) -> list:
    async def _tx(c: aiosqlite.Connection) -> list:
        cur = await c.execute(sql, params)
        return [tuple(r) for r in await cur.fetchall()]

    return await writer.submit(_tx)


@pytest.fixture
async def worker_env(tmp_path: Path):
    db_path = tmp_path / "test.db"
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.close()
    mgr = StorageConnectionManager(StorageConfig(db_path=db_path))
    writer = SQLiteWriterActor(mgr)
    await writer.start()
    yield writer
    await writer.stop()
    await mgr.drain_and_close()


@pytest.mark.asyncio
async def test_disabled_worker_does_zero_requests(worker_env):
    writer = worker_env
    worker = PriceWatchWorker(
        writer, fetcher=FakePriceFetcher([PriceFetchResult("ok", 5000)]), enabled=False
    )
    await _run_sql(
        writer,
        (
            "INSERT INTO price_watch_jobs (id, item_id, status, next_run_at) "
            "VALUES ('j1', '101', 'pending', '2026-10-23T10:00:00+00:00')"
        ),
    )
    assert await worker.poll_once("2026-10-23T11:00:00+00:00") == 0
    assert worker.requests_made == 0
    assert (
        await _run_sql(writer, "SELECT status, lease_token FROM price_watch_jobs WHERE id = 'j1'")
    )[0] == ("pending", None)


@pytest.mark.asyncio
async def test_capacity_shortage_defers_without_reducing_search_interval(worker_env):
    writer = worker_env
    worker = PriceWatchWorker(
        writer, fetcher=FakePriceFetcher(), enabled=True, spare_capacity=False, defer_sec=120
    )
    await _run_sql(
        writer,
        (
            "INSERT INTO price_watch_jobs (id, item_id, status, next_run_at) "
            "VALUES ('j2', '102', 'pending', '2026-10-23 10:00:00')"
        ),
    )
    assert await worker.poll_once("2026-10-23 11:00:00") == 0
    assert worker.jobs_deferred == 1 and worker.requests_made == 0
    row = (
        await _run_sql(writer, "SELECT status, next_run_at FROM price_watch_jobs WHERE id = 'j2'")
    )[0]
    assert row[0] == "pending" and "11:02:00" in row[1]


@pytest.mark.asyncio
async def test_fake_responses_ok_price_drop_and_rise(worker_env):
    writer = worker_env
    worker = PriceWatchWorker(
        writer,
        fetcher=FakePriceFetcher([PriceFetchResult("ok", 9000), PriceFetchResult("ok", 9500)]),
        enabled=True,
        watch_interval_sec=3600,
    )
    await _run_sql(
        writer,
        (
            "INSERT INTO query_groups (id, canonical_url, filter_fingerprint, max_price) "
            "VALUES ('q1', 'https://avito.ru/q1', 'fp1', 50000)"
        ),
    )
    await _run_sql(
        writer,
        (
            "INSERT INTO queries (id, canonical_url, bootstrap_state) "
            "VALUES ('q1', 'https://avito.ru/q1', 'ready')"
        ),
    )
    await _run_sql(
        writer,
        (
            "INSERT INTO listing_state (avito_id, known_price, price_state, revision) "
            "VALUES ('103', 10000, 'active', 1)"
        ),
    )
    await _run_sql(
        writer,
        (
            "INSERT INTO price_watch_jobs (id, item_id, query_group_id, status, next_run_at) "
            "VALUES ('j3', '103', 'q1', 'pending', '2026-10-23T10:00:00+00:00')"
        ),
    )

    assert await worker.poll_once("2026-10-23T11:00:00+00:00") == 1
    assert (
        await _run_sql(
            writer,
            (
                "SELECT kind, old_price, new_price FROM events "
                "WHERE avito_id = '103' ORDER BY revision DESC LIMIT 1"
            ),
        )
    )[0] == ("PRICE_CHANGED", 10000, 9000)

    await _run_sql(
        writer,
        "UPDATE price_watch_jobs SET next_run_at = '2026-10-23T11:30:00+00:00' WHERE id = 'j3'",
    )
    assert await worker.poll_once("2026-10-23T12:00:00+00:00") == 1
    assert (
        await _run_sql(
            writer,
            (
                "SELECT kind, old_price, new_price FROM events "
                "WHERE avito_id = '103' ORDER BY revision DESC LIMIT 1"
            ),
        )
    )[0] == ("PRICE_CHANGED", 9000, 9500)


@pytest.mark.asyncio
async def test_fake_response_404_retires_job(worker_env):
    writer = worker_env
    worker = PriceWatchWorker(
        writer,
        fetcher=FakePriceFetcher([PriceFetchResult("closed", error="HTTP 404")]),
        enabled=True,
    )
    await _run_sql(
        writer,
        (
            "INSERT INTO price_watch_jobs (id, item_id, status, next_run_at) "
            "VALUES ('j4', '104', 'pending', '2026-10-23T10:00:00+00:00')"
        ),
    )
    assert await worker.poll_once("2026-10-23T11:00:00+00:00") == 1
    assert (
        await _run_sql(
            writer, "SELECT status, lease_token, last_error FROM price_watch_jobs WHERE id = 'j4'"
        )
    )[0] == ("retired", None, "HTTP 404")


@pytest.mark.asyncio
async def test_unknown_response_retries_and_dead_letter(worker_env):
    writer = worker_env
    worker = PriceWatchWorker(
        writer,
        fetcher=FakePriceFetcher(
            [
                PriceFetchResult("unknown", error="500 error"),
                PriceFetchResult("unknown", error="timeout"),
            ]
        ),
        enabled=True,
        base_backoff_sec=60,
    )
    await _run_sql(
        writer,
        (
            "INSERT INTO price_watch_jobs (id, item_id, status, "
            "next_run_at, attempts, max_attempts) "
            "VALUES ('j5', '105', 'pending', '2026-10-23T10:00:00+00:00', 0, 2)"
        ),
    )
    await worker.poll_once("2026-10-23T10:05:00+00:00")
    assert (
        await _run_sql(
            writer, "SELECT status, attempts, last_error FROM price_watch_jobs WHERE id = 'j5'"
        )
    )[0] == ("pending", 1, "500 error")

    await _run_sql(
        writer,
        "UPDATE price_watch_jobs SET next_run_at = '2026-10-23T10:06:00+00:00' WHERE id = 'j5'",
    )
    await worker.poll_once("2026-10-23T10:10:00+00:00")
    assert (
        await _run_sql(
            writer, "SELECT status, attempts, last_error FROM price_watch_jobs WHERE id = 'j5'"
        )
    )[0] == ("dead_letter", 2, "timeout")


@pytest.mark.asyncio
async def test_shutdown_restart_unleases_jobs(worker_env):
    writer = worker_env
    worker = PriceWatchWorker(writer, enabled=True)
    await _run_sql(
        writer,
        (
            "INSERT INTO price_watch_jobs (id, item_id, status, next_run_at, "
            "lease_token, lease_until) "
            "VALUES ('j6', '106', 'leased', '2026-10-23T10:00:00+00:00', "
            "'tok1', '2026-10-23T10:30:00+00:00')"
        ),
    )
    assert await worker.release_leases() == 1
    assert (
        await _run_sql(
            writer, "SELECT status, lease_token, lease_until FROM price_watch_jobs WHERE id = 'j6'"
        )
    )[0] == ("pending", None, None)
