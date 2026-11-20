from __future__ import annotations

import json
import pytest
from unittest.mock import AsyncMock

from hunter.domain.observations import ParseOutcome
from hunter.transport.classification import (
    ResponseOutcome,
    classify_response,
)


def test_classify_response_json_items_returns_ok_with_hits():
    data = {
        "totalCount": 2,
        "catalog": {
            "items": [
                {
                    "id": 111222333,
                    "title": "Apple iPhone 15 Pro Max 256GB",
                    "priceDetailed": {"value": 85000},
                    "urlPath": "/moskva/telefony/iphone_15_pro_max_111222333",
                },
                {
                    "id": 444555666,
                    "title": "Apple iPhone 16 128GB",
                    "priceDetailed": {"value": 75000},
                    "urlPath": "/moskva/telefony/iphone_16_444555666",
                },
            ]
        },
    }
    raw = json.dumps(data)
    res = classify_response(200, raw, url="https://www.avito.ru/web/1/js/items")
    assert res.outcome == ResponseOutcome.OK
    assert len(res.hits) == 2
    assert res.hits[0].avito_id == "111222333"
    assert res.hits[0].price == 85000
    assert res.hits[1].avito_id == "444555666"


def test_classify_response_empty_json_returns_explicit_empty():
    data = {"totalCount": 0, "catalog": {"items": []}}
    raw = json.dumps(data)
    res = classify_response(200, raw, url="https://www.avito.ru/web/1/js/items")
    assert res.outcome == ResponseOutcome.EXPLICIT_EMPTY
    assert len(res.hits) == 0
