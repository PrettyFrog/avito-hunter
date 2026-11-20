"""Unit tests for exact canonical query sharing and separate fanout (S7 / T-491).

Covers:
- 100 subscriptions over 40 exact canonical queries yield exactly 40 schedules.
- Different filters (q, category, geo, radius, district, f, max_price) never merge.
- Query parameter order independence (deterministic canonicalization).
- Independent fanout per subscription (separate stopwords and personal ceilings).
"""

from __future__ import annotations

from dataclasses import dataclass

from hunter.application.query_planner import (
    CanonicalQueryPlanner,
    Subscription,
)


@dataclass
class FakeHit:
    title: str
    price: int
    description: str = ""


def test_100_subscriptions_on_40_exact_queries_yield_40_schedules():
    planner = CanonicalQueryPlanner()
    subs: list[Subscription] = []

    # Create 40 distinct base URLs
    base_urls = [
        f"https://www.avito.ru/all/audio_i_video/naushniki?q=model_{i:02d}&radius=10"
        for i in range(40)
    ]

    # Generate 100 subscriptions mapped to the 40 base URLs
    # 20 URLs have 3 subscribers each (60 subs)
    # 20 URLs have 2 subscribers each (40 subs)
    # Total = 100 subs
    sub_counter = 0
    for i, url in enumerate(base_urls):
        count = 3 if i < 20 else 2
        for _ in range(count):
            subs.append(
                Subscription(
                    subscription_id=f"sub_{sub_counter:03d}",
                    user_id=1000 + sub_counter,
                    search_url=url,
                    max_price=20000 + i * 100,
                )
            )
            sub_counter += 1

    assert len(subs) == 100

    plan = planner.plan_queries(subs)

    assert plan.total_subscriptions == 100
    assert plan.total_physical_queries == 40
    assert plan.saved_requests_per_poll == 60
    assert len(plan.physical_queries) == 40

    # Ensure every subscription is mapped to a physical query
    assert len(plan.subscription_to_query) == 100


def test_different_filters_never_merge():
    planner = CanonicalQueryPlanner()

    subs = [
        # Different query text
        Subscription("s1", 1, "https://www.avito.ru/all/telefony?q=iphone", 50000),
        Subscription("s2", 2, "https://www.avito.ru/all/telefony?q=samsung", 50000),
        # Different category
        Subscription("s3", 3, "https://www.avito.ru/all/noutbuki?q=apple", 50000),
        # Different geo radius
        Subscription("s4", 4, "https://www.avito.ru/all/telefony?q=iphone&radius=10", 50000),
        Subscription("s5", 5, "https://www.avito.ru/all/telefony?q=iphone&radius=25", 50000),
        # Different district
        Subscription("s6", 6, "https://www.avito.ru/all/telefony?q=iphone&district=12", 50000),
        # Different price ceilings
        Subscription("s7", 7, "https://www.avito.ru/all/telefony?q=iphone", 45000),
        # Unknown opaque filter param
        Subscription("s8", 8, "https://www.avito.ru/all/telefony?q=iphone&custom_f=flag_a", 50000),
        Subscription("s9", 9, "https://www.avito.ru/all/telefony?q=iphone&custom_f=flag_b", 50000),
    ]

    plan = planner.plan_queries(subs)
    # Every subscription had a distinguishing filter, so exactly 9 queries must result
    assert plan.total_physical_queries == 9
    assert plan.saved_requests_per_poll == 0


def test_query_param_order_does_not_prevent_merging():
    planner = CanonicalQueryPlanner()

    # Identical filters but query params passed in different order
    url_a = "https://www.avito.ru/all/audio_i_video?cd=1&q=sony&radius=15"
    url_b = "https://www.avito.ru/all/audio_i_video?radius=15&q=sony&cd=1"

    subs = [
        Subscription("s_order_1", 10, url_a, 15000),
        Subscription("s_order_2", 20, url_b, 15000),
    ]

    plan = planner.plan_queries(subs)
    # Successfully recognized as identical queries
    assert plan.total_physical_queries == 1
    assert plan.saved_requests_per_poll == 1
    query = plan.physical_queries[0]
    assert query.subscribers_count == 2
    assert "s_order_1" in query.subscription_ids
    assert "s_order_2" in query.subscription_ids


def test_separate_fanout_filters_personal_stopwords_and_ceilings():
    planner = CanonicalQueryPlanner()

    sub1 = Subscription(
        "sub_1",
        1,
        "https://www.avito.ru/all/telefony?q=pixel",
        40000,
        stopwords=("broken", "defect"),
    )
    sub2 = Subscription(
        "sub_2", 2, "https://www.avito.ru/all/telefony?q=pixel", 40000, stopwords=("refurbished",)
    )
    sub3 = Subscription(
        "sub_3", 3, "https://www.avito.ru/all/telefony?q=pixel", 35000, stopwords=()
    )

    all_subs = [sub1, sub2, sub3]

    # Hit 1: Clean item under all ceilings
    clean_hit = FakeHit(
        title="Google Pixel 7 Pro", price=32000, description="Great condition, like new"
    )
    res1 = planner.fanout_item(clean_hit, all_subs)
    assert set(res1) == {sub1, sub2, sub3}

    # Hit 2: Price exceeds sub3 ceiling (35000)
    expensive_hit = FakeHit(title="Google Pixel 8", price=38000, description="Box opened")
    res2 = planner.fanout_item(expensive_hit, all_subs)
    assert set(res2) == {sub1, sub2}
    assert sub3 not in res2

    # Hit 3: Contains 'broken' in title -> sub1 filtered out
    broken_hit = FakeHit(title="Google Pixel 7 broken screen", price=20000)
    res3 = planner.fanout_item(broken_hit, all_subs)
    assert set(res3) == {sub2, sub3}
    assert sub1 not in res3

    # Hit 4: Contains 'refurbished' in description -> sub2 filtered out
    refurb_hit = FakeHit(
        title="Google Pixel 7 Pro", price=30000, description="Factory refurbished device"
    )
    res4 = planner.fanout_item(refurb_hit, all_subs)
    assert set(res4) == {sub1, sub3}
    assert sub2 not in res4
