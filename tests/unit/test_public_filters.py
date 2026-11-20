"""Unit tests for public client filters (T-457).

Covers strict ceiling (no +20% headroom), boundary conditions (cap-1, cap, cap+1),
multiple categories, manual stop-word boundaries, empty stop-words, and missing/zero prices.
"""

from hunter.domain.filtering import (
    FilterVerdict,
    PublicFilter,
    evaluate_public_filters,
)
import pytest
from hunter.domain.observations import ObservedListing, PriceState


def _make_listing(
    title: str = "Test Item",
    price: int | None = 50000,
    price_state: PriceState = PriceState.ACTIVE,
    url: str = "https://avito.ru/item1",
) -> ObservedListing:
    return ObservedListing(
        avito_id="test_id_1",
        title=title,
        price=price,
        price_state=price_state,
        url=url,
    )


def test_strict_ceiling_boundaries() -> None:
    cap = 100_000
    criteria = PublicFilter(max_price=cap)

    # cap - 1 -> PASS
    res_under = evaluate_public_filters(_make_listing(price=cap - 1), criteria)
    assert res_under.passed and res_under.verdict == FilterVerdict.PASS

    # cap exact -> PASS
    res_exact = evaluate_public_filters(_make_listing(price=cap), criteria)
    assert res_exact.passed and res_exact.verdict == FilterVerdict.PASS

    # cap + 1 -> REJECTED (Zero +20% headroom!)
    res_over = evaluate_public_filters(_make_listing(price=cap + 1), criteria)
    assert not res_over.passed
    assert res_over.verdict == FilterVerdict.REJECTED_ABOVE_MAX_PRICE


def test_four_different_categories_handled_identically() -> None:
    categories = [
        ("Sony WH-1000XM5", 19_000, 20_000),  # Electronics
        ("Lada Vesta 2019", 650_000, 700_000),  # Auto
        ("1-к квартира 38м²", 4_500_000, 5_000_000),  # Real estate
        ("Зимняя куртка пуховик", 4_500, 5_000),  # Clothing/Goods
    ]
    for title, price, max_price in categories:
        criteria = PublicFilter(max_price=max_price)
        res = evaluate_public_filters(_make_listing(title=title, price=price), criteria)
        assert res.passed, f"Category item '{title}' failed evaluation"
        assert res.verdict == FilterVerdict.PASS


def test_manual_stopwords_and_word_boundaries() -> None:
    criteria = PublicFilter(max_price=100_000, manual_stop_words=("не рабочий", "блок"))

    # Word boundary match
    res_stopped1 = evaluate_public_filters(
        _make_listing(title="Телефон не рабочий на запчасти"), criteria
    )
    assert not res_stopped1.passed
    assert res_stopped1.verdict == FilterVerdict.REJECTED_STOP_WORD
    assert res_stopped1.matched_stop_word == "не рабочий"

    # Separate word matches
    res_stopped2 = evaluate_public_filters(_make_listing(title="Продам блок питания ATX"), criteria)
    assert not res_stopped2.passed
    assert res_stopped2.verdict == FilterVerdict.REJECTED_STOP_WORD

    # Substring inside another word must NOT match when boundary is enforced
    res_clean = evaluate_public_filters(_make_listing(title="iPhone заблокирован icloud"), criteria)
    # "заблокирован" does not match word "блок"
    assert res_clean.passed and res_clean.verdict == FilterVerdict.PASS


def test_empty_stop_words_allows_any_text() -> None:
    criteria = PublicFilter(max_price=50_000, manual_stop_words=())
    res = evaluate_public_filters(_make_listing(title="Любой текст без стоп-слов"), criteria)
    assert res.passed and res.verdict == FilterVerdict.PASS


def test_missing_and_zero_price_rejections() -> None:
    criteria = PublicFilter(max_price=50_000)

    # Missing price
    res_none = evaluate_public_filters(
        _make_listing(price=None, price_state=PriceState.UNKNOWN), criteria
    )
    assert not res_none.passed and res_none.verdict == FilterVerdict.REJECTED_NO_PRICE

    # Zero price (free / negotiated)
    res_zero = evaluate_public_filters(
        _make_listing(price=0, price_state=PriceState.ACTIVE), criteria
    )
    assert not res_zero.passed and res_zero.verdict == FilterVerdict.REJECTED_ZERO_PRICE


def test_inactive_price_states_rejected() -> None:
    criteria = PublicFilter(max_price=50_000)
    for state in (PriceState.DISCOUNT_TRAP, PriceState.SUSPECT, PriceState.FREE_OR_NEGOTIATED):
        res = evaluate_public_filters(_make_listing(price=10000, price_state=state), criteria)
        assert not res.passed and res.verdict == FilterVerdict.REJECTED_PRICE_NOT_ACTIVE


@pytest.mark.parametrize(
    ("title", "kw", "sw", "passed", "verdict", "matched_kw"),
    [
        ("Crucial 16GB DDR4", ("crucial", "kingston"), (), True, FilterVerdict.PASS, "crucial"),
        ("Kingston Fury", ("crucial", "kingston"), (), True, FilterVerdict.PASS, "kingston"),
        ("Samsung 16GB", ("crucial", "kingston"), (), False, FilterVerdict.REJECTED_NO_KEYWORD, None),
        ("Продам iPhone 15", ("pro",), (), False, FilterVerdict.REJECTED_NO_KEYWORD, None),
        ("iPhone 15 Pro", ("pro",), (), True, FilterVerdict.PASS, "pro"),
        ("iPhone 15 Pro на запчасти", ("pro",), ("запчасти",), False, FilterVerdict.REJECTED_STOP_WORD, None),
    ],
)
def test_manual_keywords_and_stop_words_matrix(title, kw, sw, passed, verdict, matched_kw) -> None:
    res = evaluate_public_filters(
        _make_listing(title=title),
        PublicFilter(max_price=100_000, manual_keywords=kw, manual_stop_words=sw),
    )
    assert res.passed == passed
    assert res.verdict == verdict
    assert res.matched_keyword == matched_kw


