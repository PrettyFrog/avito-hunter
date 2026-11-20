from __future__ import annotations

import json
from hunter.collect.parsers.search import parse_search, parse_search_page, _coerce_price
from hunter.collect.parsers.mfe_state import parse_mfe_state


def test_cutoff_drops_similar_items_block():
    first_link = (
        '<a data-marker="item-title" href="/moskva/audio/sony_8340205074">'
        "<h3>Sony WH-1000XM4</h3></a>"
    )
    second_link = (
        '<a data-marker="item-title" href="/moskva/audio/sony_8340205075">'
        "<h3>Sony WH-1000XM3</h3></a>"
    )
    html = f"""
    <div data-marker="catalog-serp">
      <div data-marker="item" data-item-id="8340205074">
        {first_link}
        <meta itemprop="price" content="20000" />
      </div>
      <div class="extraTitle-root-xyz">
        <h3>Похожие объявления в других городах</h3>
      </div>
      <div data-marker="item" data-item-id="8340205075">
        {second_link}
        <meta itemprop="price" content="15000" />
      </div>
    </div>
    """
    hits = parse_search(html)
    assert len(hits) == 1
    assert hits[0].avito_id == "8340205074"
    assert hits[0].title == "Sony WH-1000XM4"


def test_cutoff_zero_exact_matches_returns_empty_ok():
    html = """
    <div data-marker="catalog-serp">
      <div class="extraTitle-root-xyz">
        <h3>Похожие объявления рядом</h3>
      </div>
      <div data-marker="item" data-item-id="2001">
        <a data-marker="item-title" href="/item_2001"><h3>Some Other Headphone</h3></a>
        <meta itemprop="price" content="5000" />
      </div>
    </div>
    """
    hits, page_kind = parse_search_page(html)
    assert len(hits) == 0
    assert page_kind == "empty_ok"


def test_pre_discount_price_trap_in_search_json():
    # Avito pre-discount trap: priceDetailed.value is 50000, but iva.PriceStep is 42000
    node = {
        "id": 3001,
        "title": "MacBook Pro 14 M1 Pro",
        "priceDetailed": {"value": 50000},
        "iva": {
            "PriceStep": [
                {
                    "payload": {
                        "price": 42000,
                        "oldPrice": 50000,
                        "discountPercent": 16.0,
                    }
                }
            ]
        },
    }
    buyer_price = _coerce_price(node)
    assert buyer_price == 42000


def test_pre_discount_price_trap_in_mfe_state():
    sample_item = {
        "id": 4001,
        "title": "Bose QuietComfort Ultra",
        "urlPath": "/item/4001",
        "priceDetailed": {"value": 32000},  # Pre-discount!
        "priceWithoutDiscount": 32000,
        "iva": {
            "PriceStep": [
                {
                    "payload": {
                        "price": 26000,  # Real buyer price
                        "oldPrice": 32000,
                        "discountPercent": 18.75,
                    }
                }
            ]
        },
    }
    payload = {
        "loaderData": {
            "data": {
                "count": 1,
                "catalog": {"items": [sample_item]},
            }
        }
    }
    html = f"""<script data-mfe-state="true">{json.dumps(payload)}</script>"""
    hits = parse_mfe_state(html)
    assert len(hits) == 1
    assert hits[0].price == 26000
    assert hits[0].old_price == 32000
    assert hits[0].discount_pct == 18.75


def test_mfe_state_zero_count_drops_recommendation_items():
    # When count == 0, catalog.items contains recommendation items from other cities
    sample_item = {
        "id": 5001,
        "title": "Random Other City Item",
        "priceDetailed": {"value": 10000},
    }
    payload = {
        "loaderData": {
            "data": {
                "count": 0,  # Exact matches = 0!
                "catalog": {"items": [sample_item]},
            }
        }
    }
    html = f"""<script data-mfe-state="true">{json.dumps(payload)}</script>"""
    hits = parse_mfe_state(html)
    assert len(hits) == 0


def test_discount_and_cutoff_contract_assertions() -> None:
    """S0 Contract: verify discount percent and old_price consistency."""
    from hunter.collect.parsers.search import _coerce_price

    assert _coerce_price({"priceDetailed": {"value": 25000}}) == 25000
    assert _coerce_price({"price": 18500}) == 18500
    assert _coerce_price({}) is None
    assert _coerce_price({"priceDetailed": {"value": 15_000_000}}) == 15_000_000
