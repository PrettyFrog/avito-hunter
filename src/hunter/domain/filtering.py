"""Strict public client filters without AI, catalog matching, or headroom.

Enforces pure price ceiling (exact pmax) and manual stop-words with word boundaries.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from hunter.domain.observations import ObservedListing, PriceState


class FilterVerdict(str, Enum):
    """Evaluation outcomes for public listings."""

    PASS = "pass"
    REJECTED_NO_PRICE = "rejected_no_price"
    REJECTED_ZERO_PRICE = "rejected_zero_price"
    REJECTED_PRICE_NOT_ACTIVE = "rejected_price_not_active"
    REJECTED_ABOVE_MAX_PRICE = "rejected_above_max_price"
    REJECTED_STOP_WORD = "rejected_stop_word"
    REJECTED_NO_KEYWORD = "rejected_no_keyword"


@dataclass(frozen=True, slots=True)
class FilterResult:
    """Result of evaluating a listing against subscription filters."""

    verdict: FilterVerdict
    passed: bool
    reason: str
    matched_stop_word: str | None = None
    matched_keyword: str | None = None


@dataclass(frozen=True, slots=True)
class PublicFilter:
    """Subscription filter parameters."""

    max_price: int
    manual_stop_words: tuple[str, ...] = ()
    manual_keywords: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.max_price <= 0:
            raise ValueError(f"max_price must be positive, got {self.max_price}")


def matches_stop_word(text: str, stop_word: str) -> bool:
    """Check if stop word matches with word boundary semantics."""
    clean_word = stop_word.strip().lower()
    if not text or not clean_word:
        return False
    pattern = rf"(?:\A|\W){re.escape(clean_word)}(?:\W|\Z)"
    return bool(re.search(pattern, text.lower()))


def evaluate_public_filters(
    listing: ObservedListing,
    filter_criteria: PublicFilter,
    extra_text: str | None = None,
) -> FilterResult:
    """Evaluate listing against strict price ceiling and manual stop-words."""
    if listing.price is None:
        return FilterResult(FilterVerdict.REJECTED_NO_PRICE, False, "Listing has no price")

    if listing.price == 0:
        return FilterResult(FilterVerdict.REJECTED_ZERO_PRICE, False, "Zero price")

    if listing.price_state != PriceState.ACTIVE:
        return FilterResult(
            FilterVerdict.REJECTED_PRICE_NOT_ACTIVE,
            False,
            f"Price state {listing.price_state} is not active",
        )

    if listing.price > filter_criteria.max_price:
        return FilterResult(
            FilterVerdict.REJECTED_ABOVE_MAX_PRICE,
            False,
            f"Price {listing.price} exceeds ceiling {filter_criteria.max_price}",
        )

    combined = f"{listing.title} {extra_text or ''}".strip()
    for word in filter_criteria.manual_stop_words:
        if matches_stop_word(combined, word):
            return FilterResult(
                FilterVerdict.REJECTED_STOP_WORD,
                False,
                f"Matched stop word: '{word}'",
                matched_stop_word=word,
            )

    if filter_criteria.manual_keywords:
        matched_kw = None
        for kw in filter_criteria.manual_keywords:
            if matches_stop_word(combined, kw):
                matched_kw = kw
                break
        if not matched_kw:
            return FilterResult(
                FilterVerdict.REJECTED_NO_KEYWORD,
                False,
                "No matching keywords found",
            )
        return FilterResult(FilterVerdict.PASS, True, "OK", matched_keyword=matched_kw)

    return FilterResult(FilterVerdict.PASS, True, "OK")
