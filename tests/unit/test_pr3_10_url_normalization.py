from __future__ import annotations

import base64

from hunter.core.product import ProductProfile
from hunter.normalize.avito_url import _update_avito_f_param, normalize_avito_url


def test_update_avito_f_param_syncs_max_price_and_removes_from():
    """_update_avito_f_param synchronizes 'to' price and drops 'from' when new_from is None."""
    f_val = "ASgCAgECAUXGmgwZZHsiZnJvbSI6MTAwMDAsInRvIjoxNjAwMH0="

    updated = _update_avito_f_param(f_val, new_to=16000, new_from=None)
    assert updated is not None
    # Decode and check contents
    raw = base64.b64decode(updated + "==").decode("utf-8", errors="replace")
    assert '"to":16000' in raw or '"to": 16000' in raw
    assert "from" not in raw


def test_update_avito_f_param_preserves_min_price_when_specified():
    """_update_avito_f_param preserves 'from' when new_from is set."""
    f_val = "ASgCAgECAUXGmgwZZHsiZnJvbSI6MTAwMDAsInRvIjoxNjAwMH0="
    updated = _update_avito_f_param(f_val, new_to=18000, new_from=5000)
    assert updated is not None
    raw = base64.b64decode(updated + "==").decode("utf-8", errors="replace")
    assert '"to":18000' in raw or '"to": 18000' in raw
    assert '"from":5000' in raw or '"from": 5000' in raw


def test_normalize_avito_url_syncs_pmax_and_removes_pmin():
    """Check pmax sync, pmin removal, and f parameter alignment."""
    raw_url = "https://www.avito.ru/all/audio_i_video/naushniki-ASgCAgECAUXGmgwZZHsiZnJvbSI6MTAwMDAsInRvIjoxNjAwMH0?pmax=20000&pmin=5000&q=sony"
    clean_url, err = normalize_avito_url(raw_url, max_price=16000, headroom_pct=0.0, min_price=None)

    assert err is None
    assert "pmax=16000" in clean_url
    assert "pmin=" not in clean_url
    assert "pmax=20000" not in clean_url


def test_product_profile_auto_normalizes_search_urls_on_init():
    """ProductProfile automatically normalizes search_url on initialization."""
    raw_url = "https://www.avito.ru/all/audio_i_video/naushniki?pmax=25000&pmin=1000&q=sony+xm6"
    prof = ProductProfile(
        id="sony_test",
        name="Sony Test",
        max_price=16000,
        headroom_pct=0.0,
        search_url=raw_url,
        poll_minutes=5,
        priority="normal",
    )
    assert "pmax=16000" in prof.search_url
    assert "pmin" not in prof.search_url


def test_normalize_avito_url_deterministic_query_param_order():
    """Verify that different parameter orders normalize to exact identical canonical string."""
    url1 = "https://www.avito.ru/all/telefony?q=iphone&cd=1&radius=10"
    url2 = "https://www.avito.ru/all/telefony?radius=10&q=iphone&cd=1"
    norm1, _ = normalize_avito_url(url1, max_price=50000, headroom_pct=0.0)
    norm2, _ = normalize_avito_url(url2, max_price=50000, headroom_pct=0.0)
    assert norm1 == norm2
