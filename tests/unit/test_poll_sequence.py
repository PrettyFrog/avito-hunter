"""Durable request order survives poll-worker and application restarts."""

import asyncio
from typing import Any
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from hunter.application.poll_sequence import PollSequenceAllocator
from hunter.application.public_poll_runtime import PublicPollRuntime, QueryAssignment
from hunter.bootstrap import Bootstrap
from hunter.core.config import Settings
from hunter.core.db import Database
from hunter.domain.poll_policy import PollPolicy
from hunter.domain.slots import SlotSignal


@pytest.mark.asyncio
async def test_poll_sequence_is_global_concurrent_and_restart_safe(tmp_path) -> None:
    path = tmp_path / "sequence.db"
    settings = Settings(env="test")
    settings.public_v2.enabled = True
    first = Bootstrap(settings, db=Database(path), senders={"telegram": AsyncMock()})
    await first.startup()
    try:
        values = await asyncio.gather(*(
            first.poll_sequence.next_sequence() for _ in range(20)
        ))
        assert sorted(values) == list(range(1, 21))
    finally:
        await first.shutdown()

    second = Bootstrap(settings, db=Database(path), senders={"telegram": AsyncMock()})
    await second.startup()
    try:
        assert await second.poll_sequence.next_sequence() == 21
    finally:
        await second.shutdown()


@pytest.mark.asyncio
async def test_poll_sequence_allocator_reuses_persisted_runtime_cursor(tmp_path) -> None:
    bootstrap = Bootstrap(
        Settings(env="test"), db=Database(tmp_path / "cursor.db"),
        senders={"telegram": AsyncMock()},
    )
    await bootstrap.startup()
    try:
        allocator = PollSequenceAllocator(bootstrap.writer, key="test_cursor")
        assert await allocator.next_sequence() == 1
        replacement = PollSequenceAllocator(bootstrap.writer, key="test_cursor")
        assert await replacement.next_sequence() == 2
    finally:
        await bootstrap.shutdown()


@pytest.mark.asyncio
async def test_sequence_starts_above_existing_listing_watermark(tmp_path) -> None:
    bootstrap = Bootstrap(
        Settings(env="test"), db=Database(tmp_path / "legacy.db"),
        senders={"telegram": AsyncMock()},
    )
    await bootstrap.startup()
    try:
        async def seed(c):
            await c.execute(
                "INSERT INTO listing_state "
                "(avito_id, known_price, price_state, revision, last_request_sequence) "
                "VALUES ('legacy-lot', 100, 'active', 1, 99)"
            )
        await bootstrap.writer.submit(seed)
        allocator = PollSequenceAllocator(bootstrap.writer)
        assert await allocator.next_sequence() == 100
    finally:
        await bootstrap.shutdown()


@pytest.mark.asyncio
async def test_public_fetch_reserves_sequence_before_network_request() -> None:
    order, ingested = [], []

    class Fetcher:
        async def fetch(self, url):
            order.append("http")
            body = Path("tests/fixtures/html/search_sample.html").read_text()
            return type("Response", (), {
                "error": None, "status_code": 200, "text": body,
                "url": url, "elapsed_sec": 0.01,
            })()

    class Pool:
        def get_fetcher(self, slot_id, endpoint):
            return Fetcher()

    async def allocate():
        order.append("sequence")
        return 90

    async def ingest(batch, request_sequence):
        order.append("ingest")
        ingested.append(request_sequence)

    runtime = PublicPollRuntime(
        None, Pool(), ingest, PollPolicy(), next_sequence=allocate
    )
    assignment = QueryAssignment(
        "slot", "http://slot:8080", "query", "https://www.avito.ru/moskva", 1, 0
    )
    await runtime.poll(assignment, sequence=1)
    assert order == ["http", "sequence", "ingest"]
    assert ingested == [90]


@pytest.mark.asyncio
async def test_public_poll_runtime_accepts_unauthenticated_feed_when_require_auth_false() -> None:
    import json
    item = {"id": "2001", "title": "MacBook Pro M1", "priceDetailed": {"value": 75000}}
    loader_data = {"data": {"catalog": {"items": [item]}}}
    state = {"loaderData": loader_data, "user": {"isAuth": False, "id": 0}}
    mfe_str = json.dumps(state)
    html = f'<!DOCTYPE html><html><body><script data-mfe-state="true">{mfe_str}</script></body></html>'

    signals: list[Any] = []

    class Fetcher:
        async def fetch(self, target_url: str) -> Any:
            from types import SimpleNamespace
            return SimpleNamespace(
                status_code=200,
                text=html,
                url=target_url,
                error=None,
                elapsed_sec=0.05,
            )

    class Pool:
        def get_fetcher(self, slot_id: str, endpoint: str) -> Any:
            return Fetcher()

    ingested_batches: list[Any] = []

    async def ingest(batch: Any, request_sequence: int) -> None:
        ingested_batches.append(batch)

    async def record_signal(slot_id: str, signal: Any, **kwargs: Any) -> None:
        signals.append((slot_id, signal))

    runtime = PublicPollRuntime(
        None, Pool(), ingest, PollPolicy(),
        slot_signal_handler=record_signal,
        require_auth=False,
    )
    assignment = QueryAssignment(
        "slot", "http://slot:8080", "query", "https://www.avito.ru/moskva", 1, 0
    )
    await runtime.poll(assignment, sequence=1)
    assert len(ingested_batches) == 1
    assert not any(sig == SlotSignal.AUTH_INVALID for _, sig in signals)  # No AUTH_INVALID signal emitted

    # Counterpart: when require_auth=True, unauthenticated feed triggers signal and does not ingest
    runtime_strict = PublicPollRuntime(
        None, Pool(), ingest, PollPolicy(),
        slot_signal_handler=record_signal,
        require_auth=True,
    )
    await runtime_strict.poll(assignment, sequence=2)
    assert len(ingested_batches) == 1  # Still 1 (second was not ingested)
    auth_sigs = [sig for _, sig in signals if sig == SlotSignal.AUTH_INVALID]
    assert len(auth_sigs) == 1


