"""Regression test for F-007: Pre-allocating request_sequence before network fetch causes silent notification drops."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock
from typing import Any
import aiosqlite
import pytest

from hunter.application.public_poll_runtime import PublicPollRuntime, QueryAssignment
from hunter.domain.poll_policy import PollPolicy
from hunter.storage.observation_tx import execute_observation_tx


class MockResponse:
    error = None
    status_code = 200
    text = Path("tests/fixtures/html/search_sample.html").read_text(encoding="utf-8")
    url = "https://www.avito.ru/items"
    elapsed_sec = 0.05


class MockFetcher:
    def __init__(self, delay: float) -> None:
        self.delay = delay

    async def fetch(self, url: str) -> MockResponse:
        await asyncio.sleep(self.delay)
        return MockResponse()


async def _seed_test_db(conn: aiosqlite.Connection, schema_path: Path) -> None:
    with open(schema_path, "r", encoding="utf-8") as sf:
        schema_sql = sf.read()
    await conn.executescript(schema_sql)
    await conn.execute("INSERT INTO queries (id, canonical_url, bootstrap_state) VALUES ('q1', 'https://www.avito.ru/q1', 'ready')")
    await conn.execute("INSERT INTO queries (id, canonical_url, bootstrap_state) VALUES ('q2', 'https://www.avito.ru/q2', 'ready')")
    await conn.execute("INSERT INTO slots (id, endpoint, cookie_namespace, health_state, active_generation) VALUES ('s1', 'http://p1:8080', 'ns1', 'ready', 1)")
    await conn.execute("INSERT INTO slots (id, endpoint, cookie_namespace, health_state, active_generation) VALUES ('s2', 'http://p2:8080', 'ns2', 'ready', 1)")
    await conn.execute("INSERT INTO assignments (slot_id, query_id, generation, position, active) VALUES ('s1', 'q1', 1, 0, 1)")
    await conn.execute("INSERT INTO assignments (slot_id, query_id, generation, position, active) VALUES ('s2', 'q2', 1, 0, 1)")
    await conn.execute("INSERT INTO users (id, status, created_at) VALUES (100, 'active', '2026-10-30T00:00:00Z')")
    await conn.execute("INSERT INTO subscriptions (id, user_id, query_id, max_price, enabled) VALUES ('sub1', 100, 'q1', 50000, 1)")
    await conn.execute("INSERT INTO runtime (key, value) VALUES ('cutover:tenant:100', ?)", (json.dumps({"mode": "v2", "generation": 1}),))
    await conn.commit()


@pytest.mark.asyncio
async def test_repro_F_007_preallocated_sequence_drops_notification(clean_db_path: Path) -> None:
    schema_path = Path(__file__).resolve().parents[2] / "src/hunter/core/schema.sql"
    conn = await aiosqlite.connect(clean_db_path)
    try:
        await _seed_test_db(conn, schema_path)
        seq = 100

        async def get_next_seq() -> int:
            nonlocal seq
            seq += 1
            return seq

        http_pool = MagicMock()
        http_pool.get_fetcher.side_effect = lambda slot_id, ep: MockFetcher(0.04 if slot_id == "s1" else 0.01)

        ingest_results = []

        async def fake_ingest(batch: Any, request_sequence: int) -> Any:
            res = await execute_observation_tx(conn, batch, request_sequence=request_sequence, enforce_assignment=True)
            ingest_results.append((batch.slot_id, request_sequence, res))
            return res

        runtime = PublicPollRuntime(
            conn_mgr=None,
            http_pool=http_pool,
            ingest=fake_ingest,
            policy=PollPolicy(day_interval_sec=1, night_interval_sec=1),
            next_sequence=get_next_seq,
        )

        a1 = QueryAssignment("s1", "http://p1:8080", "q1", "https://www.avito.ru/q1", 1, 0)
        a2 = QueryAssignment("s2", "http://p2:8080", "q2", "https://www.avito.ru/q2", 1, 0)

        t1 = asyncio.create_task(runtime.poll(a1, 0))
        await asyncio.sleep(0.002)
        t2 = asyncio.create_task(runtime.poll(a2, 0))
        await asyncio.gather(t1, t2)

        s1_results = [r for r in ingest_results if r[0] == "s1"]
        assert len(s1_results) == 1
        s1_slot, s1_seq, s1_res = s1_results[0]

        assert s1_res.stale_sequence is False, f"Slow slot was rejected as stale: seq={s1_seq}, res={s1_res} (F-007)"
        assert s1_res.deliveries_created > 0, f"Delivery dropped for slow slot: seq={s1_seq} (F-007)"
    finally:
        await conn.close()
