"""Unit tests for CapacityPlan and IP cost calculations (S5 / T-500).

Covers:
- Capacity scaling across Q = 0, 1, 4, 5, 20, 40, 60, 100.
- For Q=100: 25 active + 2 reserve = 27 slots, 2700 ₽/month, 228000 search requests/day.
- Daily poll pacing: 2160 day (18h @ 30s) + 120 night (6h @ 180s) = 2280 polls/query/day.
- Configurable reserve slots and custom IP pricing.
- Separate accounting for secondary load (retries, probes, card checks).
"""

from __future__ import annotations

import pytest

from hunter.domain.capacity import (
    calculate_capacity_plan,
)


@pytest.mark.parametrize(
    "q,expected_active,expected_reserve,expected_total,expected_cost,expected_searches",
    [
        (0, 0, 2, 2, 200, 0),
        (1, 1, 2, 3, 300, 2280),
        (4, 1, 2, 3, 300, 9120),
        (5, 2, 2, 4, 400, 11400),
        (20, 5, 2, 7, 700, 45600),
        (40, 10, 2, 12, 1200, 91200),
        (60, 15, 2, 17, 1700, 136800),
        (100, 25, 2, 27, 2700, 228000),
    ],
)
def test_capacity_scaling_matrix(
    q: int,
    expected_active: int,
    expected_reserve: int,
    expected_total: int,
    expected_cost: int,
    expected_searches: int,
):
    plan = calculate_capacity_plan(queries_count=q)
    assert plan.physical_queries == q
    assert plan.active_slots == expected_active
    assert plan.reserve_slots == expected_reserve
    assert plan.total_slots == expected_total
    assert plan.total_monthly_cost_rub == expected_cost
    assert plan.search_requests_per_day == expected_searches


def test_q100_acceptance_milestone():
    """Check Q=100 capacity cost with 25 active and two reserve slots."""
    plan = calculate_capacity_plan(queries_count=100)
    assert plan.active_slots == 25
    assert plan.reserve_slots == 2
    assert plan.total_slots == 27
    assert plan.total_monthly_cost_rub == 2700
    assert plan.search_requests_per_day == 228000
    assert plan.polls_per_query_day == 2280
    assert plan.day_polls_per_query == 2160
    assert plan.night_polls_per_query == 120


def test_configurable_reserve_and_pricing():
    # Reserve = 4, price = 120 rub
    plan = calculate_capacity_plan(
        queries_count=40,
        reserve_slots=4,
        ip_monthly_rub=120,
    )
    assert plan.active_slots == 10
    assert plan.reserve_slots == 4
    assert plan.total_slots == 14
    assert plan.total_monthly_cost_rub == 14 * 120

    # Reserve = 0 (no reserve standby)
    plan_no_res = calculate_capacity_plan(queries_count=40, reserve_slots=0)
    assert plan_no_res.active_slots == 10
    assert plan_no_res.reserve_slots == 0
    assert plan_no_res.total_slots == 10
    assert plan_no_res.total_monthly_cost_rub == 1000


def test_secondary_load_breakdown():
    plan = calculate_capacity_plan(queries_count=100)
    data = plan.to_dict()

    secondary = data["secondary_load"]
    assert "probes_per_day" in secondary
    assert "retries_budget_per_day" in secondary
    assert "card_checks_per_day" in secondary

    assert plan.probes_load_per_day == 25 * 24  # 25 active slots * 24 probes
    assert plan.retries_budget_per_day == int(round(228000 * 0.02))
    assert plan.card_checks_load_per_day == int(round(228000 * 0.05))


def test_logical_subscriptions_greater_than_physical_queries():
    plan = calculate_capacity_plan(queries_count=20, subscriptions_count=50)
    assert plan.physical_queries == 20
    assert plan.logical_subscriptions == 50
    assert plan.active_slots == 5
    assert plan.search_requests_per_day == 20 * 2280
