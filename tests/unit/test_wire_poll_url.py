from __future__ import annotations

import os
import pytest
from hunter.transport.catalog_api import to_wire_poll_url


def test_to_wire_poll_url_appends_gallery_view():
    url = "https://www.avito.ru/moskva/tovary_dlya_kompyutera?cd=1&q=ddr4&s=104"
    res = to_wire_poll_url(url, prefer_gallery=True)
    assert res == "https://www.avito.ru/moskva/tovary_dlya_kompyutera?cd=1&q=ddr4&s=104&view=gallery"


def test_to_wire_poll_url_handles_no_query():
    url = "https://www.avito.ru/moskva/tovary_dlya_kompyutera"
    res = to_wire_poll_url(url, prefer_gallery=True)
    assert res == "https://www.avito.ru/moskva/tovary_dlya_kompyutera?view=gallery"


def test_to_wire_poll_url_preserves_existing_view():
    url = "https://www.avito.ru/moskva/tovary_dlya_kompyutera?view=gallery&s=104"
    res = to_wire_poll_url(url, prefer_gallery=True)
    assert res == url

    url_list = "https://www.avito.ru/moskva/tovary_dlya_kompyutera?view=list&s=104"
    res_list = to_wire_poll_url(url_list, prefer_gallery=True)
    assert res_list == url_list


def test_to_wire_poll_url_ignores_items_api():
    url = "https://www.avito.ru/web/1/js/items?categoryId=84&locationId=621540"
    res = to_wire_poll_url(url, prefer_gallery=True)
    assert res == url


def test_to_wire_poll_url_enabled_via_flag(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HUNTER_USE_GALLERY_VIEW", "1")
    url = "https://www.avito.ru/moskva/tovary_dlya_kompyutera?s=104"
    res = to_wire_poll_url(url)
    assert res == "https://www.avito.ru/moskva/tovary_dlya_kompyutera?s=104&view=gallery"


def test_to_wire_poll_url_disabled_by_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("HUNTER_USE_GALLERY_VIEW", raising=False)
    url = "https://www.avito.ru/moskva/tovary_dlya_kompyutera?s=104"
    res = to_wire_poll_url(url)
    assert res == url
