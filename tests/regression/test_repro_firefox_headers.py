"""Regression tests for Firefox Accept and Accept-Language header discrepancy."""
from __future__ import annotations

from hunter.transport.browser_headers import _default_browser_headers
from hunter.transport.http_fetch import DEFAULT_FIREFOX_USER_AGENT


def test_repro_firefox_accept_header_restores_image_formats() -> None:
    """Firefox Accept header must include image/avif and image/webp."""
    hdrs = _default_browser_headers(DEFAULT_FIREFOX_USER_AGENT)
    accept = hdrs["Accept"]
    assert "image/avif" in accept
    assert "image/webp" in accept
    assert accept == "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"


def test_repro_firefox_accept_language_restores_english_fallbacks() -> None:
    """Firefox Accept-Language must include en-US and en fallbacks."""
    hdrs = _default_browser_headers(DEFAULT_FIREFOX_USER_AGENT)
    lang = hdrs["Accept-Language"]
    assert "en-US" in lang
    assert "en;q=0.3" in lang
    assert lang == "ru-RU,ru;q=0.8,en-US;q=0.5,en;q=0.3"
