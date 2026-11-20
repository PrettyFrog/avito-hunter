#!/usr/bin/env python3
"""24-Hour Budget Simulation for Avito Hunter Multi-User.

Models:
- 4 owner products (base interval 5 min)
- 3 family users x 3 products = 9 products (base interval 15 min)
- 2 friend users x 3 products = 6 products (base interval 15 min)
Total = 19 products.

Scenarios:
1. Nominal mode (daily_cap = 5000): base intervals, planned budget = 4000 (80%).
2. Stretched mode (daily_cap = 900): tier-aware stretching, planned budget = 720 (80%).
"""

from __future__ import annotations

from hunter.core.product import ProductProfile
from hunter.budget.allocator import calculate_stretched_intervals_for_cap


def run_simulation(daily_cap: int) -> dict:
    target_budget = daily_cap * 0.80

    profiles: list[ProductProfile] = []
    user_tiers: dict[int, str] = {}

    # 4 owner products (uid 1)
    user_tiers[1] = "owner"
    for i in range(1, 5):
        pid = f"owner_prod_{i}"
        profiles.append(
            ProductProfile(
                id=pid,
                name=pid,
                search_url=f"https://avito.ru/{pid}",
                max_price=10000,
                priority="normal",
                poll_minutes=5,
                owner_user_id=1,
            )
        )

    # 3 family users x 3 products (uid 11, 12, 13)
    for u in range(1, 4):
        uid = 10 + u
        user_tiers[uid] = "family"
        for p in range(1, 4):
            pid = f"family_u{u}_p{p}"
            profiles.append(
                ProductProfile(
                    id=pid,
                    name=pid,
                    search_url=f"https://avito.ru/{pid}",
                    max_price=10000,
                    priority="normal",
                    poll_minutes=15,
                    owner_user_id=uid,
                )
            )

    # 2 friend users x 3 products (uid 21, 22)
    for u in range(1, 3):
        uid = 20 + u
        user_tiers[uid] = "friend"
        for p in range(1, 4):
            pid = f"friend_u{u}_p{p}"
            profiles.append(
                ProductProfile(
                    id=pid,
                    name=pid,
                    search_url=f"https://avito.ru/{pid}",
                    max_price=10000,
                    priority="normal",
                    poll_minutes=15,
                    owner_user_id=uid,
                )
            )

    # Calculate intervals
    stretched, planned_polls = calculate_stretched_intervals_for_cap(
        profiles,
        target_cap=daily_cap,
        target_ratio=0.80,
        user_tiers=user_tiers,
    )

    base_daily = sum(1440.0 / (p.poll_minutes or 30) for p in profiles)

    # Simulate 24 hours (1440 minutes, minute by minute)
    polls_per_product: dict[str, int] = {p.id: 0 for p in profiles}
    hourly_polls = [0] * 24

    for minute in range(1440):
        hour = minute // 60
        for p in profiles:
            interval = stretched.get(p.id, p.poll_minutes or 30)
            if minute % interval == 0:
                polls_per_product[p.id] += 1
                hourly_polls[hour] += 1

    total_polls = sum(polls_per_product.values())

    # Verify invariants
    min_polls = min(polls_per_product.values())
    assert min_polls > 0, f"Starvation detected! Min polls = {min_polls}"
    assert total_polls <= target_budget * 1.05, (
        f"Budget exceeded: {total_polls} > {target_budget * 1.05}"
    )

    return {
        "daily_cap": daily_cap,
        "target_budget": target_budget,
        "base_daily": base_daily,
        "stretched_intervals": stretched,
        "polls_per_product": polls_per_product,
        "hourly_polls": hourly_polls,
        "total_polls": total_polls,
        "min_polls": min_polls,
        "max_polls": max(polls_per_product.values()),
    }


def main() -> None:
    print("=" * 70)
    print("AVITO HUNTER 24-HOUR MULTI-USER BUDGET SIMULATION")
    print("=" * 70)

    for cap in (5000, 900):
        res = run_simulation(cap)
        print(
            f"\n--- Scenario: daily_cap = {cap} (80% planned budget = "
            f"{res['target_budget']:.0f}) ---"
        )
        print(f"Base daily requests: {res['base_daily']:.1f}")
        print(f"Total simulated polls: {res['total_polls']}")
        print(f"Product polls range: min={res['min_polls']}, max={res['max_polls']}")

        # Sample intervals by tier
        intervals = res["stretched_intervals"]
        owner_int = intervals["owner_prod_1"]
        family_int = intervals["family_u1_p1"]
        friend_int = intervals["friend_u1_p1"]
        print(f"Sample intervals: owner={owner_int}m, family={family_int}m, friend={friend_int}m")

        # Hourly distribution
        print("Hourly distribution (h0..h23):")
        chunks = [res["hourly_polls"][i : i + 6] for i in range(0, 24, 6)]
        for idx, chunk in enumerate(chunks):
            start_h = idx * 6
            print(f"  Hours {start_h:02d}-{start_h + 5:02d}: {chunk}")

    print("\n✅ Simulation completed successfully! All invariants hold.")


if __name__ == "__main__":
    main()
