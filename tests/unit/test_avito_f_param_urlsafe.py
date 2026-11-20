"""TDD regressions for Avito URL-safe base64 encoding in 'f' filter parameter."""

import pytest
from hunter.application.subscription_rebind import change_query_price
from hunter.domain.query_compatibility import _extract_f_non_price
from hunter.domain.query_url import (
    ConflictingPriceError,
    extract_f_price_ceiling,
    sanitize_f_param,
    validate_and_canonicalize_query_url,
)
from hunter.normalize.avito_url import _update_avito_f_param

# Real Avito f-params from user report (containing underscore '_' from apple slug)
F_APPLE_100K = "ASgBAgECAkS0wA3OqzmwwQ2I_DcBRcaaDBZ7ImZyb20iOjAsInRvIjoxMDAwMDB9"
F_APPLE_40K = "ASgBAgECAkS0wA3OqzmwwQ2I_DcBRcaaDBV7ImZyb20iOjAsInRvIjo0MDAwMH0"


def test_extract_f_price_ceiling_handles_urlsafe_characters():
    ceiling = extract_f_price_ceiling(F_APPLE_100K)
    assert ceiling == 100000, f"Expected 100000, got {ceiling}"


def test_sanitize_f_param_rewrites_price_with_urlsafe_characters():
    sanitized = sanitize_f_param(F_APPLE_100K, 30000)
    assert extract_f_price_ceiling(sanitized) == 30000
    assert "_" in sanitized, "Must preserve URL-safe underscore in slug"


def test_update_avito_f_param_preserves_urlsafe_and_updates_bounds():
    updated = _update_avito_f_param(F_APPLE_100K, new_to=30000)
    assert extract_f_price_ceiling(updated) == 30000
    assert "_" in updated


def test_extract_f_non_price_handles_urlsafe_characters():
    non_price, ceiling = _extract_f_non_price(F_APPLE_100K)
    assert ceiling == 100000
    assert non_price is not None


def test_conflicting_f_price_detected_on_urlsafe_param():
    url = (
        f"https://www.avito.ru/all/telefony/mobilnye_telefony/apple-ASgBAgICAkS0wA3OqzmwwQ2I_Dc"
        f"?cd=1&f={F_APPLE_100K}&s=104"
    )
    # Must reject conflicting price 30000 != 100000
    with pytest.raises(ConflictingPriceError, match="Conflicting price in f parameter: 100000 != 30000"):
        validate_and_canonicalize_query_url(url, max_price=30000)


def test_change_query_price_sanitizes_urlsafe_f_param():
    url = (
        f"https://www.avito.ru/all/telefony/mobilnye_telefony/apple-ASgBAgICAkS0wA3OqzmwwQ2I_Dc"
        f"?cd=1&f={F_APPLE_100K}&s=104&pmax=100000"
    )
    rebound = change_query_price(url, 30000)
    assert "pmax=30000" in rebound
    new_f = rebound.split("f=")[1].split("&")[0]
    assert extract_f_price_ceiling(new_f) == 30000
    assert "_" in new_f
