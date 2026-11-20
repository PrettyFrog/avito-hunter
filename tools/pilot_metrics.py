#!/usr/bin/env python3
"""Calculate pilot latency, throughput, and delivery metrics from Avito Hunter database."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any


def calculate_latencies(rows: list[tuple[str, str]]) -> dict[str, float]:
    """Calculates min, p50, p95, and max latency in seconds between observed_at and sent_at."""
    deltas: list[float] = []
    for obs_str, sent_str in rows:
        try:
            obs = datetime.fromisoformat(obs_str)
            sent = datetime.fromisoformat(sent_str)
            sec = max(0.0, (sent - obs).total_seconds())
            deltas.append(sec)
        except Exception:
            continue

    if not deltas:
        return {"min": 0.0, "p50": 0.0, "p95": 0.0, "max": 0.0, "count": 0}

    deltas.sort()
    n = len(deltas)
    p50_idx = int(0.50 * (n - 1))
    p95_idx = int(0.95 * (n - 1))

    return {
        "min": round(deltas[0], 2),
        "p50": round(deltas[p50_idx], 2),
        "p95": round(deltas[p95_idx], 2),
        "max": round(deltas[-1], 2),
        "count": n,
    }


def query_pilot_metrics(db_path: Path) -> dict[str, Any]:
    """Extracts summary metrics from SQLite tables."""
    if not db_path.exists():
        raise FileNotFoundError(f"Database file not found: {db_path}")

    conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    try:
        cur = conn.cursor()

        # 1. Total events
        cur.execute("SELECT COUNT(*) FROM events")
        total_events = cur.fetchone()[0]

        # 2. Deliveries by status
        cur.execute("SELECT status, COUNT(*) FROM deliveries GROUP BY status")
        status_counts = dict(cur.fetchall())
        total_deliveries = sum(status_counts.values())

        # 3. Latency sample for sent deliveries
        query = """
            SELECT e.observed_at, d.available_at
            FROM deliveries d
            JOIN events e ON d.event_id = e.id
            WHERE d.status = 'sent' AND e.observed_at IS NOT NULL
        """
        cur.execute(query)
        latency_rows = cur.fetchall()
        latency_stats = calculate_latencies(latency_rows)

        # 4. Outbox backlog (oldest pending)
        cur.execute(
            "SELECT MIN(created_at) FROM deliveries WHERE status = 'pending'"
        )
        oldest_pending = cur.fetchone()[0]

        # 5. Active slots & health
        cur.execute("SELECT health, COUNT(*) FROM slots GROUP BY health")
        slots_health = dict(cur.fetchall())

        success_rate = 0.0
        if total_deliveries > 0:
            sent_count = status_counts.get("sent", 0)
            success_rate = round((sent_count / total_deliveries) * 100, 2)

        return {
            "total_events": total_events,
            "total_deliveries": total_deliveries,
            "deliveries_by_status": status_counts,
            "delivery_success_rate_pct": success_rate,
            "latency_seconds": latency_stats,
            "oldest_pending_created_at": oldest_pending,
            "slots_by_health": slots_health,
        }
    finally:
        conn.close()


def format_text_report(metrics: dict[str, Any]) -> str:
    """Renders human-readable pilot metrics summary."""
    lines = [
        "===========================================================",
        "            Avito Hunter Pilot Metrics Report              ",
        "===========================================================",
        f"Total Ingested Events:     {metrics['total_events']}",
        f"Total Outbox Deliveries:   {metrics['total_deliveries']}",
        f"Delivery Success Rate:     {metrics['delivery_success_rate_pct']}%",
        f"Deliveries By Status:      {metrics['deliveries_by_status']}",
        "",
        "Delivery Latency (seconds):",
        f"  Min:  {metrics['latency_seconds']['min']} s",
        f"  p50:  {metrics['latency_seconds']['p50']} s",
        f"  p95:  {metrics['latency_seconds']['p95']} s",
        f"  Max:  {metrics['latency_seconds']['max']} s",
        f"  Count evaluated: {metrics['latency_seconds']['count']}",
        "",
        f"Oldest Pending Delivery:   {metrics['oldest_pending_created_at'] or 'None (backlog empty)'}",
        f"Slots By Health:           {metrics['slots_by_health'] or 'None'}",
        "===========================================================",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Pilot metrics and latency analysis")
    parser.add_argument("--db", type=Path, required=True, help="Path to SQLite database")
    parser.add_argument("--json", action="store_true", help="Output in JSON format")
    args = parser.parse_args()

    try:
        metrics = query_pilot_metrics(args.db)
        if args.json:
            print(json.dumps(metrics, indent=2))
        else:
            print(format_text_report(metrics))
        return 0
    except Exception as exc:
        print(f"Error extracting metrics: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
