"""Pure domain model for slot capacity planning, IP costs, and daily query load (S5 / T-500).

Contracts:
- Physical queries Q partitioned into ceil(Q / 4) active slots.
- Reserve slots accounted separately (configurable, default 2).
- Cost model: 100 ₽/IP/month applied to total (active + reserve) slots.
- 24h search budget: 18h Day (30s) + 6h Night (180s) = 2280 polls/query/day.
- Q=100 -> 25 active + 2 reserve = 27 slots, 2700 ₽/month, 228000 search requests/day.
- Secondary load (probes, retries, card checks) displayed as distinct overhead lines.
- Pure calculation without benchmark/network side effects.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

DEFAULT_SLOT_CAPACITY = 4
DEFAULT_RESERVE_SLOTS = 2
DEFAULT_IP_MONTHLY_RUB = 100
DEFAULT_DAY_HOURS = 18.0
DEFAULT_NIGHT_HOURS = 6.0
DEFAULT_DAY_INTERVAL_SEC = 30.0
DEFAULT_NIGHT_INTERVAL_SEC = 180.0


@dataclass(frozen=True, slots=True)
class CapacityPlan:
    physical_queries: int
    logical_subscriptions: int
    active_slots: int
    reserve_slots: int
    total_slots: int
    ip_monthly_rub: int
    total_monthly_cost_rub: int
    search_requests_per_day: int
    polls_per_query_day: int
    day_polls_per_query: int
    night_polls_per_query: int
    probes_load_per_day: int
    retries_budget_per_day: int
    card_checks_load_per_day: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "physical_queries": self.physical_queries,
            "logical_subscriptions": self.logical_subscriptions,
            "active_slots": self.active_slots,
            "reserve_slots": self.reserve_slots,
            "total_slots": self.total_slots,
            "ip_monthly_rub": self.ip_monthly_rub,
            "total_monthly_cost_rub": self.total_monthly_cost_rub,
            "search_requests_per_day": self.search_requests_per_day,
            "polls_per_query_day": self.polls_per_query_day,
            "day_polls_per_query": self.day_polls_per_query,
            "night_polls_per_query": self.night_polls_per_query,
            "secondary_load": {
                "probes_per_day": self.probes_load_per_day,
                "retries_budget_per_day": self.retries_budget_per_day,
                "card_checks_per_day": self.card_checks_load_per_day,
            },
        }


def calculate_capacity_plan(
    queries_count: int,
    subscriptions_count: int | None = None,
    *,
    slot_capacity: int = DEFAULT_SLOT_CAPACITY,
    reserve_slots: int = DEFAULT_RESERVE_SLOTS,
    ip_monthly_rub: int = DEFAULT_IP_MONTHLY_RUB,
    day_hours: float = DEFAULT_DAY_HOURS,
    night_hours: float = DEFAULT_NIGHT_HOURS,
    day_interval_sec: float = DEFAULT_DAY_INTERVAL_SEC,
    night_interval_sec: float = DEFAULT_NIGHT_INTERVAL_SEC,
    retries_ratio: float = 0.02,
    probes_per_slot_day: int = 24,
    card_checks_ratio: float = 0.05,
) -> CapacityPlan:
    """Calculates active slots, reserve overhead, monthly IP cost, and daily request volume."""
    q = max(int(queries_count), 0)
    subs = max(int(subscriptions_count), 0) if subscriptions_count is not None else q
    cap = max(int(slot_capacity), 1)
    res = max(int(reserve_slots), 0)
    price_ip = max(int(ip_monthly_rub), 0)

    # Active slots required to handle Q physical queries
    active = math.ceil(q / cap) if q > 0 else 0
    total_slots = active + res
    monthly_cost = total_slots * price_ip

    # Daily poll pacing per query: Day (18h @ 30s) + Night (6h @ 180s)
    day_polls = int(round(day_hours * 3600.0 / day_interval_sec))
    night_polls = int(round(night_hours * 3600.0 / night_interval_sec))
    polls_per_query = day_polls + night_polls

    search_requests = q * polls_per_query
    retries = int(round(search_requests * retries_ratio))
    probes = active * probes_per_slot_day
    cards = int(round(search_requests * card_checks_ratio))

    return CapacityPlan(
        physical_queries=q,
        logical_subscriptions=subs,
        active_slots=active,
        reserve_slots=res,
        total_slots=total_slots,
        ip_monthly_rub=price_ip,
        total_monthly_cost_rub=monthly_cost,
        search_requests_per_day=search_requests,
        polls_per_query_day=polls_per_query,
        day_polls_per_query=day_polls,
        night_polls_per_query=night_polls,
        probes_load_per_day=probes,
        retries_budget_per_day=retries,
        card_checks_load_per_day=cards,
    )
