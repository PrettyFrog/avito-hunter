from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from hunter.core.db import Database
from hunter.core.models import now_utc


@pytest.mark.asyncio
async def test_log_request_auto_error_and_get_recent_errors(tmp_path: Path) -> None:
    db_path = tmp_path / "test_errors.db"
    db = Database(db_path)
    await db.connect()

    # 1. Log various requests
    # Normal 200 request (should not appear in errors)
    await db.log_request(
        product_id="sony_xm6",
        req_type="search_render",
        status=200,
        url="https://avito.ru/search1",
        latency_ms=1200,
        error=None,
    )

    # 502 error with error=None -> should automatically set error="http_502"
    await db.log_request(
        product_id="sony_xm6",
        req_type="search_render",
        status=502,
        url="https://avito.ru/search2",
        latency_ms=4500,
        error=None,
    )

    # 429 error with failover message
    await db.log_request(
        product_id="u1_iphone_17_256",
        req_type="search_light",
        status=429,
        url="https://avito.ru/search3",
        latency_ms=800,
        error="http_429:profile_1:failover",
    )

    # Timeout error (status=0, error="timeout")
    await db.log_request(
        product_id="bose_quietcomfort_ultra_gen_2",
        req_type="card",
        status=0,
        url="https://avito.ru/card1",
        latency_ms=30000,
        error="timeout",
    )

    # Another 502 for bose
    await db.log_request(
        product_id="bose_quietcomfort_ultra_gen_2",
        req_type="search_render",
        status=502,
        url="https://avito.ru/search4",
        latency_ms=5000,
        error="http_502",
    )

    # 2. Query recent errors
    res = await db.get_recent_request_errors(since_hours=24, limit=50)

    assert res["total_count"] == 4
    assert res["by_status"].get("HTTP 502") == 2
    assert res["by_status"].get("HTTP 429") == 1
    assert res["by_status"].get("timeout") == 1

    assert res["by_watch"].get("bose_quietcomfort_ultra_gen_2") == 2
    assert res["by_watch"].get("sony_xm6") == 1
    assert res["by_watch"].get("u1_iphone_17_256") == 1

    events = res["events"]
    assert len(events) == 4
    # Check that events have product_id, status, error
    assert events[0]["product_id"] == "bose_quietcomfort_ultra_gen_2"
    assert events[0]["status"] == 502


@pytest.mark.asyncio
async def test_get_recent_request_errors_window_filter(tmp_path: Path) -> None:
    db_path = tmp_path / "test_window.db"
    db = Database(db_path)
    conn = await db.connect()

    # Old event (30 hours ago)
    old_ts = (now_utc() - timedelta(hours=30)).isoformat()
    await conn.execute(
        """
        INSERT INTO request_log (ts, product_id, req_type, status, url, latency_ms, error)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (old_ts, "old_watch", "search", 502, "https://avito.ru/old", 5000, "http_502"),
    )

    # Recent event (2 hours ago)
    recent_ts = (now_utc() - timedelta(hours=2)).isoformat()
    await conn.execute(
        """
        INSERT INTO request_log (ts, product_id, req_type, status, url, latency_ms, error)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (recent_ts, "recent_watch", "search", 502, "https://avito.ru/recent", 5000, "http_502"),
    )
    await conn.commit()

    res_24h = await db.get_recent_request_errors(since_hours=24, limit=50)
    assert res_24h["total_count"] == 1
    assert res_24h["events"][0]["product_id"] == "recent_watch"

    res_48h = await db.get_recent_request_errors(since_hours=48, limit=50)
    assert res_48h["total_count"] == 2
