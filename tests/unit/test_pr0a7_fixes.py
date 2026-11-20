from __future__ import annotations

from hunter.collect.parsers.search import _is_json_empty, parse_search_page


def test_is_json_empty_nested_counters_not_empty() -> None:
    """Проверяет, что вложенные счетчики не превращают непустую выдачу в пустую."""
    # Real-world metadata samples from Avito preloadedState
    blob_favorites = {
        "layout": {
            "header": {
                "headerInfo": {
                    "favoritesUnreadCounter": 0,
                    "favoritesUnreadCounterDetailed": {"searches": 0, "sellers": 0},
                    "rating": {"activeReviewsCount": 0},
                }
            }
        }
    }
    assert _is_json_empty(blob_favorites) is False

    blob_balance = {"user": {"account": {"balance": {"total": 0, "bonus": 0, "real": 0}}}}
    assert _is_json_empty(blob_balance) is False

    blob_items_with_total_zero = {"total": 0, "items": [{"id": 12345, "title": "Real Item"}]}
    assert _is_json_empty(blob_items_with_total_zero) is False


def test_is_json_empty_known_paths() -> None:
    """Проверяет корректное распознавание пустой выдачи по известным путям состояния каталога."""
    # 1. Top-level catalog empty
    assert _is_json_empty({"total": 0, "items": []}) is True
    assert _is_json_empty({"count": 0, "items": []}) is True
    assert _is_json_empty({"totalCount": 0, "items": []}) is True

    # 2. catalog scope
    assert _is_json_empty({"catalog": {"total": 0, "items": []}}) is True
    assert _is_json_empty({"catalog": {"count": 0, "items": []}}) is True

    # 3. data.catalog scope
    assert _is_json_empty({"data": {"catalog": {"count": 0, "items": []}}}) is True

    # 4. serp scope
    assert _is_json_empty({"serp": {"total": 0, "items": []}}) is True


def test_parse_search_deleted_cards_returns_unknown_empty() -> None:
    """Удалённые карточки без маркера пустой выдачи дают unknown_empty."""
    html = """
    <!DOCTYPE html>
    <html>
    <body>
        <div data-marker="catalog-serp">
            <!-- Items were removed due to selector break or layout change -->
        </div>
        <div>Смотрите также другие разделы каталога</div>
    </body>
    </html>
    """
    # Without explicit empty marker or empty JSON, must classify as unknown_empty
    hits, state = parse_search_page(html)
    assert len(hits) == 0
    assert state == "unknown_empty"


def test_parse_search_catalog_serp_total_zero_returns_empty_ok() -> None:
    """Страница с catalog-serp и total: 0 по известному пути JSON -> empty_ok."""
    html = """
    <!DOCTYPE html>
    <html>
    <body>
        <div data-marker="catalog-serp"></div>
    </body>
    </html>
    """
    extra_json = [{"catalog": {"total": 0, "items": []}}]
    hits, state = parse_search_page(html, extra_json=extra_json)
    assert len(hits) == 0
    assert state == "empty_ok"
