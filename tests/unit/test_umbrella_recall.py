"""Unit tests for umbrella query admission with proof of recall (T-492)."""

from hunter.application.umbrella import (
    SyntheticItem,
    UmbrellaAdmissionController,
    UmbrellaCandidate,
)


def test_umbrella_default_experimental_off() -> None:
    ctrl = UmbrellaAdmissionController(experimental_enabled=False)
    cand = UmbrellaCandidate("cand_1", "https://www.avito.ru/moskva/telefony?q=iphone", 50000)
    items = [SyntheticItem("1", "iPhone 12", 45000)]
    dec = ctrl.evaluate_admission(cand, narrow_items=items, wide_items=items)
    assert dec.admitted is False and "experimental mode is disabled" in dec.reason


def test_umbrella_admitted_with_full_recall() -> None:
    ctrl = UmbrellaAdmissionController(experimental_enabled=True)
    cand = UmbrellaCandidate("cand_2", "https://www.avito.ru/moskva/telefony?q=iphone", 50000)
    narrow = [SyntheticItem("n1", "iPhone 11", 30000), SyntheticItem("n2", "iPhone 12", 45000)]
    wide = [
        SyntheticItem("w_cheap", "iPhone SE", 15000),
        SyntheticItem("n1", "iPhone 11", 30000),
        SyntheticItem("n2", "iPhone 12", 45000),
        SyntheticItem("w_exp", "iPhone 14 Pro", 80000),
    ]
    dec = ctrl.evaluate_admission(cand, narrow_items=narrow, wide_items=wide)
    assert dec.admitted is True and dec.recall is not None and dec.recall.is_complete is True
    assert dec.recall.lost_ids == () and dec.recall.recall_rate == 1.0
    eligible_ids = {it.id for it in dec.eligible_bundle}
    assert {"n1", "n2"}.issubset(eligible_ids) and "w_exp" not in eligible_ids


def test_umbrella_rejected_on_lost_tail_item() -> None:
    ctrl = UmbrellaAdmissionController(experimental_enabled=True)
    cand = UmbrellaCandidate("cand_3", "https://www.avito.ru/moskva/noutbuki?q=macbook", 70000)
    narrow = [
        SyntheticItem("m1", "MacBook Air M1", 60000),
        SyntheticItem("m_tail", "MacBook Pro 13", 55000),
    ]
    wide = [
        SyntheticItem("m1", "MacBook Air M1", 60000),
        SyntheticItem("other", "Lenovo Yoga", 40000),
    ]
    dec = ctrl.evaluate_admission(cand, narrow_items=narrow, wide_items=wide)
    assert dec.admitted is False and dec.recall is not None and dec.recall.lost_ids == ("m_tail",)
    assert "Lost 1 tail item" in dec.reason


def test_umbrella_rejected_on_opaque_filter() -> None:
    ctrl = UmbrellaAdmissionController(experimental_enabled=True)
    cand = UmbrellaCandidate(
        "cand_4", "https://www.avito.ru/moskva/do-50000/telefony?q=iphone", 50000
    )
    items = [SyntheticItem("1", "iPhone", 40000)]
    dec = ctrl.evaluate_admission(cand, narrow_items=items, wide_items=items)
    assert dec.admitted is False and "Opaque filter" in dec.reason


def test_umbrella_saturation_with_spare_capacity_recommends_split() -> None:
    ctrl = UmbrellaAdmissionController(experimental_enabled=True, window_size=5)
    cand = UmbrellaCandidate("cand_5", "https://www.avito.ru/moskva/audio?q=bose", 20000)
    narrow = [SyntheticItem("b1", "Bose QC35", 15000)]
    wide = [SyntheticItem("b1", "Bose QC35", 15000)] + [
        SyntheticItem(f"b_{i}", f"Bose {i}", 15000) for i in range(1, 5)
    ]
    dec = ctrl.evaluate_admission(cand, narrow_items=narrow, wide_items=wide, spare_capacity=2)
    assert dec.admitted is False and dec.is_saturated is True and dec.can_split is True
    assert "split recommended" in dec.reason


def test_umbrella_saturation_without_spare_capacity_reports_degraded() -> None:
    ctrl = UmbrellaAdmissionController(experimental_enabled=True, window_size=5)
    cand = UmbrellaCandidate("cand_6", "https://www.avito.ru/moskva/audio?q=sony", 20000)
    narrow = [SyntheticItem("s1", "Sony XM4", 18000)]
    wide = [SyntheticItem("s1", "Sony XM4", 18000)] + [
        SyntheticItem(f"s_{i}", f"Sony {i}", 18000) for i in range(1, 5)
    ]
    dec = ctrl.evaluate_admission(cand, narrow_items=narrow, wide_items=wide, spare_capacity=0)
    assert dec.admitted is False and dec.is_saturated is True and dec.can_split is False
    assert "Degraded: saturated window with no spare capacity" in dec.reason


def test_umbrella_sponsored_listings_trigger_saturation() -> None:
    ctrl = UmbrellaAdmissionController(experimental_enabled=True, window_size=20)
    cand = UmbrellaCandidate("cand_7", "https://www.avito.ru/moskva/audio?q=jbl", 10000)
    narrow = [SyntheticItem("j1", "JBL Flip", 7000)]
    wide = [SyntheticItem("j1", "JBL Flip", 7000)] + [
        SyntheticItem(f"sp_{i}", f"Sponsored {i}", 8000, is_sponsored=True) for i in range(12)
    ]
    dec = ctrl.evaluate_admission(cand, narrow_items=narrow, wide_items=wide, spare_capacity=1)
    assert dec.admitted is False and dec.is_saturated is True
