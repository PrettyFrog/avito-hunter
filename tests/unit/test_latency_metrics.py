"""Tests for latency metrics provenance, health read models, and secrets redaction (S7 / T-493)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import aiosqlite
import pytest

from hunter.application.metrics import (
    EventLatencyProvenance,
    LowCardinalityMetricsCollector,
    classify_cache_freshness,
    classify_latency_bucket,
)
from hunter.application.public_poll_runtime import PublicPollRuntime, QueryAssignment
from hunter.domain.poll_policy import PollPolicy
from hunter.storage.read_models import fetch_system_health, redact_secrets


def test_provenance_stages_and_deterministic_buckets():
    obs, com, acc = (
        "2026-10-23T12:00:00+00:00",
        "2026-10-23T12:00:02+00:00",
        "2026-10-23T12:00:15+00:00",
    )
    prov = EventLatencyProvenance(
        published_at="2026-10-23T11:59:00+00:00", observed_at=obs, committed_at=com, accepted_at=acc
    )
    assert (
        prov.compute_publication_lag() == 60.0
        and prov.compute_ingest_lag() == 2.0
        and prov.compute_delivery_lag() == 13.0
        and prov.compute_e2e_lag() == 15.0
    )

    buckets = [classify_latency_bucket(v) for v in (5.0, 15.0, 45.0, 120.0, 600.0, 1200.0, None)]
    assert buckets == ["<10s", "10-30s", "30-60s", "1-5m", "5-15m", ">15m", "unknown"]


def test_collector_percentiles_and_state_separation():
    coll = LowCardinalityMetricsCollector()
    for val in [5.0, 10.0, 20.0, 50.0, 100.0]:
        coll.record_latency(val)
    assert coll.get_percentile(0.50) == 20.0 and coll.get_percentile(0.95) == 100.0

    for st in ("active", "degraded", "bootstrap", "waiting"):
        coll.record_state(st)
    assert coll.state_counts["degraded"] == 1 and coll.state_counts["active"] == 1
    dist = coll.get_bucket_distribution()
    assert dist["<10s"] == 1 and dist["10-30s"] == 2 and dist["30-60s"] == 1 and dist["1-5m"] == 1
    coll.record_queue_age(4.5)
    coll.record_delivery_provenance(EventLatencyProvenance(
        committed_at="2026-10-27T12:00:00+00:00",
        accepted_at="2026-10-27T12:00:07+00:00",
    ))
    assert list(coll.queue_age_samples) == [4.5] and list(coll.delivery_lag_samples) == [7.0]


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"Age": "0", "Cache-Control": "max-age=60", "ETag": '"secret-tag"'}, "fresh"),
        ({"Date": "Tue, 27 Oct 2026 12:00:00 GMT", "Cache-Control": "max-age=60"}, "fresh"),
        ({"Age": "3", "Cache-Control": "max-age=60"}, "cached"),
        ({"Age": "90", "Cache-Control": "max-age=60"}, "stale"),
    ],
)
def test_cache_headers_distinguish_fresh_cached_and_stale_without_values(headers, expected):
    state, _, safe_headers = classify_cache_freshness(
        headers, datetime(2026, 10, 27, 12, tzinfo=UTC)
    )
    assert state == expected
    assert "etag" in safe_headers if "ETag" in headers else "etag" not in safe_headers


@pytest.mark.asyncio
async def test_public_poll_metrics_count_slot_and_redact_correlation_context():
    metrics = LowCardinalityMetricsCollector()

    class Fetcher:
        async def fetch(self, url):
            return SimpleNamespace(
                error=None, status_code=200,
                text=Path("tests/fixtures/html/search_sample.html").read_text(),
                url=url, elapsed_sec=0.01,
                headers={"Age": "2", "ETag": '"private-cache-tag"'},
            )

    class Pool:
        def get_fetcher(self, slot_id, endpoint):
            return Fetcher()

    async def ingest(batch, request_sequence):
        return None

    runtime = PublicPollRuntime(
        None, Pool(), ingest, PollPolicy(), metrics=metrics,
    )
    assignment = QueryAssignment(
        "slot-42", "http://proxy", "private-query", "https://www.avito.ru/moskva?q=secret", 1, 0
    )
    await runtime.poll(assignment, sequence=1)
    assert metrics.slot_requests["slot-42"] == 1
    sample = metrics.poll_samples[-1]
    assert (sample.cache_state, sample.source_visibility) == ("cached", "visible")
    assert sample.cache_age_sec == 2.0
    assert len(sample.correlation_id) == 32 and sample.cache_headers == ("age", "etag")
    assert "private-query" not in repr(sample) and "secret" not in repr(sample)

    metrics.record_poll(
        slot_id="slot-42", status_code=0, error=True, headers={},
        source_visibility="unavailable", publication_lag_sec=None, duration_sec=2.0,
    )
    assert metrics.slot_errors["slot-42"] == 1


@pytest.mark.asyncio
async def test_fetch_system_health_and_secrets_redaction():
    conn = await aiosqlite.connect(":memory:")
    await conn.executescript(

                                     '\n        CREATE TABLE queries (id TEXT'
                                     ' PRIMARY KEY, bootstrap_state TEXT);\n '
                                     '       CREATE TABLE deliveries (id TEX'
                                     'T PRIMARY KEY, status TEXT, created_at'
                                     ' TEXT);\n        CREATE TABLE slots (id'
                                     ' TEXT PRIMARY KEY, enabled INTEGER, ne'
                                     'xt_check TEXT);\n        INSERT INTO qu'
                                     "eries (id, bootstrap_state) VALUES ('q"
                                     "1', 'active'), ('q2', 'degraded'), ('q"
                                     "3', 'pending');\n        INSERT INTO de"
                                     'liveries (id, status, created_at) VALU'
                                     "ES ('d1', 'pending', '2026-10-23T11:50"
                                     ":00+00:00');\n        INSERT INTO slots"
                                     " (id, enabled, next_check) VALUES ('s1"
                                     "', 1, '2026-10-23 11:58:00');\n    "

                             )
    await conn.commit()

    health = await fetch_system_health(
        conn, now_dt=datetime(2026, 10, 23, 12, 0, 0, tzinfo=UTC)
    )
    assert (
        health.active_queries == 1
        and health.degraded_queries == 1
        and health.bootstrap_queries == 1
    )
    assert health.oldest_pending_delivery_age_sec == 600.0 and health.slot_schedule_lag_sec >= 120.0
    await conn.close()

    redacted = redact_secrets("token=secret_123 cookie=sess_456 other")
    assert (
        "secret_123" not in redacted and "sess_456" not in redacted and "***REDACTED***" in redacted
    )
