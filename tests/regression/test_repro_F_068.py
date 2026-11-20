"""Regression test for F-068: Corrupted 404 URLs from search parser concatenation.

When base_url contains query parameters (e.g. ?cd=1&q=macbook&s=104),
parsers must not concatenate base_url with relative item href/urlPath,
which produces corrupted URLs like:
https://www.avito.ru/moskva/noutbuki?cd=1&q=macbook&s=104/moskva/noutbuki/apple_123
"""

from __future__ import annotations

import json
from hunter.collect.parsers.mfe_state import parse_mfe_state
from hunter.collect.parsers.search import parse_search, _hit_from_json


def test_html_search_parser_url_resolution_with_query_params() -> None:
    html = """
    <div data-marker="item" data-item-id="4829104812">
        <a data-marker="item-title" href="/moskva/noutbuki/apple_macbook_pro_16_4829104812" title="Apple MacBook Pro 16">
            <h3>Apple MacBook Pro 16</h3>
        </a>
        <meta itemprop="price" content="95000" />
    </div>
    """
    search_url = "https://www.avito.ru/moskva/noutbuki?cd=1&q=macbook&s=104"
    hits = parse_search(html, base_url=search_url)

    assert len(hits) == 1
    expected = "https://www.avito.ru/moskva/noutbuki/apple_macbook_pro_16_4829104812"
    assert hits[0].url == expected
    assert "?" not in hits[0].url


def test_json_search_parser_url_resolution_with_query_params() -> None:
    search_url = "https://www.avito.ru/moskva/noutbuki?cd=1&q=macbook&s=104"
    node = {
        "id": "4829104812",
        "title": "Apple MacBook Pro 16",
        "urlPath": "/moskva/noutbuki/apple_macbook_pro_16_4829104812",
        "priceDetailed": {"value": 95000},
    }
    hit = _hit_from_json(node, search_url)
    assert hit is not None
    assert hit.url == "https://www.avito.ru/moskva/noutbuki/apple_macbook_pro_16_4829104812"

    # Also test empty urlPath fallback to /item/{id}
    node_no_url = {
        "id": "4829104812",
        "title": "Apple MacBook Pro 16",
        "priceDetailed": {"value": 95000},
    }
    hit2 = _hit_from_json(node_no_url, search_url)
    assert hit2 is not None
    assert hit2.url == "https://www.avito.ru/item/4829104812"


def test_mfe_state_parser_url_resolution_with_query_params() -> None:
    search_url = "https://www.avito.ru/moskva/noutbuki?cd=1&q=macbook&s=104"
    mfe_data = {
        "loaderData": {
            "data": {
                "catalog": {
                    "items": [
                        {
                            "id": "4829104812",
                            "title": "Apple MacBook Pro 16",
                            "urlPath": "/moskva/noutbuki/apple_macbook_pro_16_4829104812",
                            "priceDetailed": {"value": 95000},
                        }
                    ]
                }
            }
        }
    }
    html = f"""
    <html>
    <body>
    <script type="application/json" data-mfe-state="true">{json.dumps(mfe_data)}</script>
    </body>
    </html>
    """
    hits = parse_mfe_state(html, base_url=search_url)
    assert len(hits) == 1
    assert hits[0].url == "https://www.avito.ru/moskva/noutbuki/apple_macbook_pro_16_4829104812"
