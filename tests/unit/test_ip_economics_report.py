"""Tests for query economics reporting and cost savings (S7 / T-512)."""

from __future__ import annotations

import pytest
import aiosqlite

from hunter.storage.read_models import compute_query_economics, fetch_query_economics


def test_query_economics_calculation_exact():
    # 100 subscriptions, 40 physical queries, 4 queries/slot, 100 rub/slot
    m = compute_query_economics(
        logical_subs=100,
        physical_queries=40,
        reserve_slots=2,
        queries_per_slot=4,
        cost_per_slot_monthly=100.0,
    )
    assert m.active_slots == 10
    assert m.reserve_slots == 2
    assert m.total_monthly_cost == 1200.0  # 10 active + 2 reserve = 12 * 100 = 1200 ₽
    assert m.no_sharing_monthly_cost == 2700.0  # 25 active + 2 reserve = 27 * 100 = 2700 ₽
    assert m.cost_savings == 1500.0
    assert m.request_savings_pct == 60.0


def test_idle_service_configurable_reserve():
    # Idle service with 0 queries
    m = compute_query_economics(
        logical_subs=0,
        physical_queries=0,
        reserve_slots=3,
        queries_per_slot=4,
        cost_per_slot_monthly=100.0,
    )
    assert m.active_slots == 0
    assert m.reserve_slots == 3
    assert m.total_monthly_cost == 300.0


def test_role_visibility_and_disclaimer():
    ip_details = [{"id": "slot_1", "endpoint": "192.168.1.10", "status": "ok"}]

    # Non-owner view: IP pool details hidden
    user_view = compute_query_economics(
        logical_subs=10, physical_queries=5, ip_pool_details=ip_details, is_owner=False
    )
    assert user_view.ip_pool_details is None
    assert "не является оплаченным счётом" in user_view.disclaimer

    # Owner view: IP pool details visible
    owner_view = compute_query_economics(
        logical_subs=10, physical_queries=5, ip_pool_details=ip_details, is_owner=True
    )
    assert owner_view.ip_pool_details is not None
    assert owner_view.ip_pool_details[0]["endpoint"] == "192.168.1.10"


@pytest.mark.asyncio
async def test_fetch_query_economics_db():
    conn = await aiosqlite.connect(":memory:")
    await conn.executescript("""
        CREATE TABLE subscriptions (id TEXT PRIMARY KEY, enabled INTEGER);
        CREATE TABLE queries (id TEXT PRIMARY KEY);
        CREATE TABLE slots (id TEXT PRIMARY KEY, endpoint TEXT, health_state TEXT, role TEXT);
        INSERT INTO subscriptions (id, enabled) VALUES ('s1', 1), ('s2', 1), ('s3', 1), ('s4', 0);
        INSERT INTO queries (id) VALUES ('q1'), ('q2');
        INSERT INTO slots (id, endpoint, health_state, role) VALUES ('slot1',\
 'http://proxy.host:8080?token=sec123', 'ok', 'active'), ('slot2',\
 'http://proxy2.host:8080', 'quarantined', 'active');\n\
    """)
    await conn.commit()

    econ = await fetch_query_economics(
        conn, is_owner=True, cost_per_slot_monthly=100.0, reserve_slots=2, queries_per_slot=2
    )
    assert econ.logical_subscriptions == 3  # only enabled=1
    assert econ.physical_queries == 2
    assert econ.active_slots == 1
    assert econ.quarantine_slots == 1
    assert econ.ip_pool_details is not None
    # Verify token is redacted in IP details
    assert "sec123" not in econ.ip_pool_details[0]["endpoint"]
    assert "***REDACTED***" in econ.ip_pool_details[0]["endpoint"]
    await conn.close()
