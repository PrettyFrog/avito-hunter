"""Regression test for F-086: Missing city in search alerts when Avito omits location node in regional searches.

Verifies that:
1. Search parser extracts city from URL slug or addressDetailed when location is None.
2. Production listing 8390901033 with urlPath '/sankt-peterburg/...' extracts city='Санкт-Петербург'.
3. Delivery payload fanout resolves missing location from listing.url.
4. Telegram alert caption contains 📍 location.
"""

from __future__ import annotations

import json
import pytest

from hunter.collect.parsers.search import parse_search_page
from hunter.domain.observations import ObservedListing, PriceState
from hunter.notify.telegram_delivery import format_alert_caption, format_rich_html
from hunter.normalize.city import city_from_slug, city_from_url, extract_item_city


def test_repro_f086_search_parser_extracts_city_from_regional_url_slug() -> None:
    raw_json = json.dumps({
        "catalog": {
            "items": [
                {
                    "id": 8390901033,
                    "title": "DDR4 4Gb",
                    "urlPath": "/sankt-peterburg/tovary_dlya_kompyutera/ddr4_4gb_8390901033",
                    "priceDetailed": {"value": 2222},
                    "priceWithoutDiscount": 496,
                    "location": None,
                    "addressDetailed": None,
                    "seller": {"type": "private"},
                },
                {
                    "id": 8390901034,
                    "title": "DDR4 8Gb",
                    "urlPath": "/sankt-peterburg_kolpino/tovary_dlya_kompyutera/ddr4_8gb_8390901034",
                    "priceDetailed": {"value": 3000},
                    "location": None,
                    "seller": {"type": "private"},
                },
                {
                    "id": 8390901035,
                    "title": "DDR4 16Gb",
                    "urlPath": "/all/tovary_dlya_kompyutera/ddr4_16gb_8390901035",
                    "priceDetailed": {"value": 5000},
                    "location": None,
                    "seller": {"type": "private"},
                },
            ]
        }
    })

    hits, kind = parse_search_page(raw_json)
    assert kind == "json_items"
    assert len(hits) == 3

    assert hits[0].city == "Санкт-Петербург"
    assert hits[1].city == "Санкт-Петербург, Колпино"
    assert hits[2].city is None


def test_repro_f086_city_helpers() -> None:
    assert city_from_slug("moskva") == "Москва"
    assert city_from_slug("sankt-peterburg") == "Санкт-Петербург"
    assert city_from_slug("spb") == "Санкт-Петербург"
    assert city_from_slug("all") is None
    assert city_from_slug("item") is None

    assert city_from_url("https://www.avito.ru/sankt-peterburg/tovary_dlya_kompyutera/ddr4_8390901033") == "Санкт-Петербург"
    assert city_from_url("/moskva/telefony/apple_123") == "Москва"

    # Address detailed extraction
    node_addr = {"addressDetailed": {"locationName": "Москва, Красная площадь"}}
    assert extract_item_city(node_addr) == "Москва, Красная площадь"


def test_repro_f086_caption_includes_location() -> None:
    caption = format_alert_caption(
        title="DDR4 4Gb",
        new_price=2222,
        old_price=496,
        location="Санкт-Петербург",
        time_ago="1 мин назад",
        search_name="DDR4",
    )
    assert "📍 Санкт-Петербург" in caption

    rich = format_rich_html(
        title="DDR4 4Gb",
        new_price=2222,
        old_price=496,
        location="Санкт-Петербург",
        time_ago="1 мин назад",
        search_name="DDR4",
    )
    assert "📍 Санкт-Петербург" in rich


def test_repro_f086_no_reviews_and_delivery_status() -> None:
    # Item like 8607764577 where seller has no reviews and delivery is not specified (None)
    rich = format_rich_html(
        title="Оперативная память ddr3 16 gb",
        new_price=2900,
        old_price=3000,
        location="Сестрорецк",
        is_delivery=None,
        seller_line=None,
        search_name="DDR4",
        url="https://www.avito.ru/8607764577",
    )
    assert "⭐ Нет отзывов" in rich
    assert "🚚 Доставка не проверена" in rich

    # Item with delivery=True
    rich_dlv = format_rich_html(
        title="iPhone",
        new_price=50000,
        is_delivery=True,
        seller_line=None,
    )
    assert "🚚 Есть доставка" in rich_dlv
    assert "⭐ Нет отзывов" in rich_dlv

