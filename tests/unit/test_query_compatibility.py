"""Unit tests for query compatibility and multi-ceiling price grouping (S7 / T-502).

Covers:
- 20k / 25k / 30k subscriptions merged into 1 query with 30k ceiling (no +20%).
- Individual lots strictly filtered by each subscription's personal price ceiling.
- Different cities, radius, sort, delivery flags never merge.
- Unparseable 'f' parameter or price-in-path prohibits grouping.
- Manual stopwords applied locally per subscription.
"""

from __future__ import annotations

from hunter.domain.query_compatibility import (
    CompatibleSubscription,
    QueryCompatibility,
)


def test_20k_25k_30k_merged_into_single_30k_query_without_synthetic_headroom():
    qc = QueryCompatibility()

    sub1 = CompatibleSubscription(
        "sub_20k", 1, "https://www.avito.ru/moskva/telefony?q=iphone", 20000
    )
    sub2 = CompatibleSubscription(
        "sub_25k", 2, "https://www.avito.ru/moskva/telefony?q=iphone", 25000
    )
    sub3 = CompatibleSubscription(
        "sub_30k", 3, "https://www.avito.ru/moskva/telefony?q=iphone", 30000
    )

    groups = qc.group_subscriptions([sub1, sub2, sub3])

    assert len(groups) == 1
    grp = groups[0]
    assert grp.group_ceiling == 30000
    assert grp.member_count == 3
    assert "pmax=30000" in grp.base_url
    # Ensure no synthetic +20% (36000) was added
    assert "pmax=36000" not in grp.base_url

    # Fanout evaluation of a 23 000 ₽ item
    # sub1 (20 000 max): REJECTED
    assert qc.filter_for_subscription(23000, "iPhone 11", sub1) is False
    # sub2 (25 000 max): ACCEPTED
    assert qc.filter_for_subscription(23000, "iPhone 11", sub2) is True
    # sub3 (30 000 max): ACCEPTED
    assert qc.filter_for_subscription(23000, "iPhone 11", sub3) is True


def test_different_cities_never_merge():
    qc = QueryCompatibility()

    sub_msk = CompatibleSubscription(
        "sub_msk", 1, "https://www.avito.ru/moskva/telefony?q=macbook", 60000
    )
    sub_spb = CompatibleSubscription(
        "sub_spb", 2, "https://www.avito.ru/sankt-peterburg/telefony?q=macbook", 70000
    )

    assert qc.are_compatible(sub_msk.search_url, 60000, sub_spb.search_url, 70000) is False

    groups = qc.group_subscriptions([sub_msk, sub_spb])
    assert len(groups) == 2


def test_different_radius_never_merge():
    qc = QueryCompatibility()

    sub_r10 = CompatibleSubscription(
        "sub_r10", 1, "https://www.avito.ru/all/audio_i_video?q=sony&radius=10", 15000
    )
    sub_r25 = CompatibleSubscription(
        "sub_r25", 2, "https://www.avito.ru/all/audio_i_video?q=sony&radius=25", 20000
    )

    assert qc.are_compatible(sub_r10.search_url, 15000, sub_r25.search_url, 20000) is False

    groups = qc.group_subscriptions([sub_r10, sub_r25])
    assert len(groups) == 2


def test_different_sort_never_merge():
    qc = QueryCompatibility()

    sub_s104 = CompatibleSubscription(
        "sub_s104", 1, "https://www.avito.ru/all/audio_i_video?q=sony&s=104", 15000
    )
    sub_s101 = CompatibleSubscription(
        "sub_s101", 2, "https://www.avito.ru/all/audio_i_video?q=sony&s=101", 20000
    )

    assert qc.are_compatible(sub_s104.search_url, 15000, sub_s101.search_url, 20000) is False

    groups = qc.group_subscriptions([sub_s104, sub_s101])
    assert len(groups) == 2


def test_different_delivery_flags_never_merge():
    qc = QueryCompatibility()

    sub_d1 = CompatibleSubscription(
        "sub_d1", 1, "https://www.avito.ru/all/audio_i_video?q=sony&d=1", 15000
    )
    sub_nod = CompatibleSubscription(
        "sub_nod", 2, "https://www.avito.ru/all/audio_i_video?q=sony", 20000
    )

    assert qc.are_compatible(sub_d1.search_url, 15000, sub_nod.search_url, 20000) is False

    groups = qc.group_subscriptions([sub_d1, sub_nod])
    assert len(groups) == 2


def test_unparseable_f_prohibits_grouping():
    qc = QueryCompatibility()

    bad_f_url = "https://www.avito.ru/all/telefony?f=invalid_not_base64_json!@#$"
    valid_url = "https://www.avito.ru/all/telefony?q=iphone"

    sub_bad = CompatibleSubscription("sub_bad", 1, bad_f_url, 20000)
    sub_ok = CompatibleSubscription("sub_ok", 2, valid_url, 25000)

    assert qc.are_compatible(sub_bad.search_url, 20000, sub_ok.search_url, 25000) is False

    groups = qc.group_subscriptions([sub_bad, sub_ok])
    assert len(groups) == 2
    # sub_bad is solo unmergeable
    assert any("solo" in g.group_id for g in groups)


def test_price_in_path_prohibits_grouping():
    qc = QueryCompatibility()

    path_price_url = "https://www.avito.ru/moskva/telefony/do-30000"
    normal_url = "https://www.avito.ru/moskva/telefony?pmax=30000"

    sub_path = CompatibleSubscription("sub_path", 1, path_price_url, 30000)
    sub_norm = CompatibleSubscription("sub_norm", 2, normal_url, 30000)

    assert qc.are_compatible(sub_path.search_url, 30000, sub_norm.search_url, 30000) is False

    groups = qc.group_subscriptions([sub_path, sub_norm])
    assert len(groups) == 2


def test_manual_stopwords_evaluated_locally():
    qc = QueryCompatibility()

    sub_words = CompatibleSubscription(
        "sub_w", 1, "https://www.avito.ru/all/telefony", 30000, stopwords=("разбит", "дефект")
    )
    sub_clean = CompatibleSubscription(
        "sub_c", 2, "https://www.avito.ru/all/telefony", 30000, stopwords=()
    )

    # Item with defect
    assert qc.filter_for_subscription(20000, "iPhone 11 разбит экран", sub_words) is False
    assert qc.filter_for_subscription(20000, "iPhone 11 разбит экран", sub_clean) is True
