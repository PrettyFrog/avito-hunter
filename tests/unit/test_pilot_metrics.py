from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import pytest

from tools.pilot_metrics import calculate_latencies, format_text_report, query_pilot_metrics


def test_calculate_latencies_handles_empty_and_valid() -> None:
    empty = calculate_latencies([])
    assert empty["count"] == 0
    assert empty["p50"] == 0.0

    sample = [
        ("2026-10-30T10:00:00+00:00", "2026-10-30T10:00:10+00:00"),  # 10s
        ("2026-10-30T10:00:00+00:00", "2026-10-30T10:00:20+00:00"),  # 20s
        ("2026-10-30T10:00:00+00:00", "2026-10-30T10:00:30+00:00"),  # 30s
    ]
    res = calculate_latencies(sample)
    assert res["count"] == 3
    assert res["min"] == 10.0
    assert res["p50"] == 20.0
    assert res["max"] == 30.0


def test_query_pilot_metrics_missing_db_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        query_pilot_metrics(tmp_path / "nonexistent.db")


def test_query_pilot_metrics_with_seeded_db(tmp_path: Path) -> None:
    db_file = tmp_path / "test_pilot.db"
    conn = sqlite3.connect(db_file)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE events (
            id TEXT PRIMARY KEY,
            observed_at TEXT
        );
    """)
    cur.execute("""
        CREATE TABLE deliveries (
            id TEXT PRIMARY KEY,
            event_id TEXT,
            status TEXT,
            available_at TEXT,
            created_at TEXT
        );
    """)
    cur.execute("""
        CREATE TABLE slots (
            slot_id TEXT PRIMARY KEY,
            health TEXT
        );
    """)

    # Seed events
    cur.execute("INSERT INTO events VALUES ('e1', '2026-10-30T12:00:00+00:00')")
    cur.execute("INSERT INTO events VALUES ('e2', '2026-10-30T12:05:00+00:00')")

    # Seed deliveries
    cur.execute("INSERT INTO deliveries VALUES ('d1', 'e1', 'sent', '2026-10-30T12:00:15+00:00', '2026-10-30T12:00:05+00:00')")
    cur.execute("INSERT INTO deliveries VALUES ('d2', 'e2', 'pending', '2026-10-30T12:05:00+00:00', '2026-10-30T12:05:00+00:00')")

    # Seed slots
    cur.execute("INSERT INTO slots VALUES ('slot_1', 'READY')")
    conn.commit()
    conn.close()

    metrics = query_pilot_metrics(db_file)
    assert metrics["total_events"] == 2
    assert metrics["total_deliveries"] == 2
    assert metrics["deliveries_by_status"] == {"sent": 1, "pending": 1}
    assert metrics["delivery_success_rate_pct"] == 50.0
    assert metrics["latency_seconds"]["count"] == 1
    assert metrics["latency_seconds"]["p50"] == 15.0
    assert metrics["oldest_pending_created_at"] == "2026-10-30T12:05:00+00:00"
    assert metrics["slots_by_health"] == {"READY": 1}

    report = format_text_report(metrics)
    assert "Avito Hunter Pilot Metrics Report" in report
    assert "Total Ingested Events:     2" in report
