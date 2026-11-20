"""Read models for latency provenance, system health, and safe reporting (S7 / T-493)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import aiosqlite


def redact_secrets(text: str) -> str:
    """Sanitizes text by masking session cookies, api tokens, and auth secrets."""
    for pat in (r"(token=)[^\s&]+", r"(cookie=)[^\s;]+", r"(sessid=)[^\s;]+", r"(secret=)[^\s&]+"):
        text = re.sub(pat, r"\1***REDACTED***", text, flags=re.I)
    return text


def redact_proxy_endpoint(endpoint: str) -> str:
    """Expose only a valid proxy host and port; never reveal URL credentials or query data."""
    try:
        parsed = urlsplit(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return "[redacted]"
        host = parsed.hostname
        if ":" in host:
            host = f"[{host}]"
        port = f":{parsed.port}" if parsed.port is not None else ""
        hidden = (
            parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment
        )
        suffix = "/***REDACTED***" if hidden else ""
        return f"{parsed.scheme}://{host}{port}{suffix}"
    except ValueError:
        return "[redacted]"


@dataclass(frozen=True, slots=True)
class SystemHealthReadModel:
    slot_schedule_lag_sec: float | None = None
    oldest_pending_delivery_age_sec: float | None = None
    degraded_queries: int = 0
    bootstrap_queries: int = 0
    active_queries: int = 0
    waiting_queries: int = 0


@dataclass(frozen=True, slots=True)
class LatencySummaryReadModel:
    count: int = 0
    p50_sec: float = 0.0
    p95_sec: float = 0.0
    p99_sec: float = 0.0
    max_sec: float = 0.0
    buckets: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class QueryEconomicsReadModel:
    logical_subscriptions: int
    physical_queries: int
    active_slots: int
    reserve_slots: int
    quarantine_slots: int
    cost_per_slot_monthly: float
    total_monthly_cost: float
    no_sharing_monthly_cost: float
    cost_savings: float
    request_savings_pct: float
    ip_pool_details: list[dict[str, Any]] | None = None
    disclaimer: str = "Расчёт является ориентировочной оценкой экономии пула и не является оплаченным счётом."


def compute_query_economics(
    logical_subs: int,
    physical_queries: int,
    reserve_slots: int = 2,
    quarantine_slots: int = 0,
    queries_per_slot: int = 4,
    cost_per_slot_monthly: float = 100.0,
    ip_pool_details: list[dict[str, Any]] | None = None,
    is_owner: bool = False,
) -> QueryEconomicsReadModel:
    """Calculates query sharing economics, slot capacity, and monthly cost estimations."""
    import math
    active = math.ceil(physical_queries / queries_per_slot) if physical_queries > 0 else 0
    no_share_active = math.ceil(logical_subs / queries_per_slot) if logical_subs > 0 else 0
    total_cost = (active + reserve_slots) * cost_per_slot_monthly
    no_share_cost = (no_share_active + reserve_slots) * cost_per_slot_monthly
    savings = max(0.0, no_share_cost - total_cost)
    pct = round((1.0 - (physical_queries / logical_subs)) * 100, 1) if (logical_subs > 0 and physical_queries <= logical_subs) else 0.0
    pool = ip_pool_details if is_owner else None
    return QueryEconomicsReadModel(
        logical_subscriptions=logical_subs, physical_queries=physical_queries,
        active_slots=active, reserve_slots=reserve_slots, quarantine_slots=quarantine_slots,
        cost_per_slot_monthly=cost_per_slot_monthly, total_monthly_cost=total_cost,
        no_sharing_monthly_cost=no_share_cost, cost_savings=savings, request_savings_pct=pct,
        ip_pool_details=pool,
    )


async def fetch_query_economics(
    conn: aiosqlite.Connection,
    is_owner: bool = False,
    cost_per_slot_monthly: float = 100.0,
    reserve_slots: int = 2,
    queries_per_slot: int = 4,
) -> QueryEconomicsReadModel:
    cur = await conn.execute("SELECT count(*) FROM subscriptions WHERE enabled = 1")
    s_count = (await cur.fetchone())[0]
    cur = await conn.execute("SELECT count(*) FROM queries")
    q_count = (await cur.fetchone())[0]
    cur = await conn.execute("SELECT id, endpoint, health_state FROM slots")
    rows = await cur.fetchall()
    quarantine_count = sum(1 for r in rows if str(r[2]).lower() == "quarantined")
    ip_details = None
    if is_owner:
        ip_details = [
            {"id": r[0], "endpoint": redact_proxy_endpoint(str(r[1])), "status": r[2]}
            for r in rows
        ]

    return compute_query_economics(
        logical_subs=s_count, physical_queries=q_count,
        reserve_slots=reserve_slots, quarantine_slots=quarantine_count,
        queries_per_slot=queries_per_slot, cost_per_slot_monthly=cost_per_slot_monthly,
        ip_pool_details=ip_details, is_owner=is_owner,
    )


async def fetch_system_health(conn: aiosqlite.Connection, now_dt: datetime | None = None) -> SystemHealthReadModel:
    """Assembles unified system health snapshot across queries, slots, and deliveries."""
    now_ts = (now_dt or datetime.now(UTC)).timestamp()
    st_counts: dict[str, int] = {"degraded": 0, "bootstrap": 0, "active": 0, "waiting": 0}
    cur = await conn.execute("SELECT bootstrap_state, count(*) FROM queries GROUP BY bootstrap_state")
    for st, cnt in await cur.fetchall():
        k = (st or "").lower()
        key = "bootstrap" if k in ("pending", "bootstrap") else (k if k in st_counts else "active")
        st_counts[key] += cnt

    oldest_age: float | None = None
    cur = await conn.execute("SELECT MIN(created_at) FROM deliveries WHERE status = 'pending'")
    row = await cur.fetchone()
    if row and row[0]:
        dt = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
        oldest_age = max(0.0, now_ts - (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).timestamp())

    max_lag: float | None = None
    cur = await conn.execute("PRAGMA table_info(slots)")
    columns = {row[1] for row in await cur.fetchall()}
    if {"enabled", "next_check"} <= columns:
        cur = await conn.execute("SELECT MAX(? - strftime('%s', next_check)) FROM slots WHERE enabled = 1", (now_ts,))
        row = await cur.fetchone()
        if row and row[0] is not None:
            max_lag = max(0.0, float(row[0]))

    return SystemHealthReadModel(
        slot_schedule_lag_sec=round(max_lag, 2) if max_lag is not None else None,
        oldest_pending_delivery_age_sec=round(oldest_age, 2) if oldest_age is not None else None,
        degraded_queries=st_counts["degraded"],
        bootstrap_queries=st_counts["bootstrap"],
        active_queries=st_counts["active"],
        waiting_queries=st_counts["waiting"],
    )
