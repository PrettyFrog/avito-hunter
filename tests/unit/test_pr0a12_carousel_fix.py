from hunter.collect.parsers.search import parse_search_page, price_missing_ratio

HTML_TWO_ITEMS = """
<!DOCTYPE html>
<html>
<body>
<div data-marker="catalog-serp">
    <div data-marker="item" data-item-id="1000000001">
        <a data-marker="item-title" href="/item/1000000001"><span>Sony WH-1000XM4 Black</span></a>
        <span data-marker="item-price"><meta itemprop="price" content="15000">15 000 ₽</span>
    </div>
    <div data-marker="item" data-item-id="1000000002">
        <a data-marker="item-title" href="/item/1000000002"><span>Sony WH-1000XM4 Silver</span></a>
        <span data-marker="item-price"><meta itemprop="price" content="16000">16 000 ₽</span>
    </div>
</div>
</body>
</html>
"""

SYNTHETIC_XHR_EXTRA = [
    {
        "catalog": {
            "items": [
                {
                    "id": "2000000001",
                    "title": "Sony WH-1000XM4 Black 1",
                    "urlPath": "/item/2000000001",
                    # No price
                },
                {
                    "id": "2000000002",
                    "title": "Sony WH-1000XM4 Black 2",
                    "urlPath": "/item/2000000002",
                    # No price
                },
                {
                    "id": "2000000003",
                    "title": "Sony WH-1000XM4 Black 3",
                    "urlPath": "/item/2000000003",
                    # No price
                },
            ]
        }
    }
]

def test_xhr_items_cannot_create_hits_outside_dom_and_embedded_state():
    """
    Rule 1.1: Source of truth is HTML (DOM grid + embedded state).
    Items from intercepted XHR (extra_json) MUST NOT create hits.
    They can only enrich fields for avito_id already present in DOM/embedded state.
    Discarded items must be counted in carousel_discarded.
    """
    hits, page_kind = parse_search_page(
        HTML_TWO_ITEMS,
        base_url="https://www.avito.ru",
        extra_json=SYNTHETIC_XHR_EXTRA,
    )

    hit_ids = [h.avito_id for h in hits]
    # Only the 2 DOM items should be hits
    assert hit_ids == ["1000000001", "1000000002"], f"Unexpected hits on rc9: {hit_ids}"
    assert len(hits) == 2
    assert price_missing_ratio(hits) == 0.0
    # 3 XHR items outside DOM must be counted in carousel_discarded
    assert getattr(hits, "carousel_discarded", 0) == 3


def test_xhr_items_can_enrich_existing_dom_hit():
    """
    Items from intercepted XHR (extra_json) CAN enrich fields for avito_id
    that already exists in DOM (e.g. seller_type, snippet).
    """
    xhr_enrich = [
        {
            "catalog": {
                "items": [
                    {
                        "id": "1000000001",
                        "title": "Sony WH-1000XM4 Black (enriched)",
                        "urlPath": "/item/1000000001",
                        "seller": {"kind": "private"},
                        "description": "Enriched description from XHR",
                    }
                ]
            }
        }
    ]

    hits, page_kind = parse_search_page(
        HTML_TWO_ITEMS,
        base_url="https://www.avito.ru",
        extra_json=xhr_enrich,
    )

    assert len(hits) == 2
    hit1 = next(h for h in hits if h.avito_id == "1000000001")
    assert hit1.seller_type == "private"
    assert hit1.snippet == "Enriched description from XHR"


def test_xhr_items_ignored_when_dom_grid_empty():
    """
    If DOM grid is empty, XHR lots are ignored and page is classified as empty.
    """
    empty_html = """
    <!DOCTYPE html>
    <html>
    <body>
    <div data-marker="catalog-serp"></div>
    <div data-marker="empty-page">Ничего не найдено</div>
    </body>
    </html>
    """

    hits, page_kind = parse_search_page(
        empty_html,
        base_url="https://www.avito.ru",
        extra_json=SYNTHETIC_XHR_EXTRA,
    )

    assert len(hits) == 0
    assert page_kind == "empty_ok"
    assert getattr(hits, "carousel_discarded", 0) == 3


def test_real_bose_ultra_xhr_fixture():
    """
    Real-data fixture test (Requirement 1.3):
    Verifies on actual post-deploy sample from production server that:
    1. hits == DOM items (2 items)
    2. price_missing_ratio == 0.0
    3. The 8 XHR recommendation items outside DOM do not create listings and are discarded.
    """
    import gzip
    import json
    from pathlib import Path

    fixture_dir = Path(__file__).resolve().parent.parent / "fixtures" / "html"
    html_file = fixture_dir / "real_bose_ultra_search.html.gz"
    xhr_file = fixture_dir / "real_bose_ultra_xhr.json.gz"

    assert html_file.exists(), f"Missing fixture {html_file}"
    assert xhr_file.exists(), f"Missing fixture {xhr_file}"

    html = gzip.decompress(html_file.read_bytes()).decode("utf-8")
    xhr_data = json.loads(gzip.decompress(xhr_file.read_bytes()).decode("utf-8"))

    hits, page_kind = parse_search_page(
        html,
        base_url="https://www.avito.ru",
        extra_json=xhr_data,
    )

    assert page_kind == "grid"
    assert len(hits) == 2
    assert getattr(hits, "dom_hits", 0) == 2
    assert getattr(hits, "xhr_extra", 0) == 8
    assert getattr(hits, "carousel_discarded", 0) == 8
    assert price_missing_ratio(hits) == 0.0

