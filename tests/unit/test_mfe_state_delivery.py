from __future__ import annotations

import json

from hunter.collect.parsers.mfe_state import parse_mfe_state


def _mfe_html(item: dict) -> str:
    payload = {
        "loaderData": {
            "data": {
                "count": 1,
                "catalog": {"items": [item]},
            }
        }
    }
    return f'<script data-mfe-state="true">{json.dumps(payload)}</script>'


def _item(**extra: object) -> dict:
    item = {
        "id": 8452471098,
        "title": "Комплект Материнская плата Machinist X79/16GB/E5",
        "urlPath": "/moskva/tovary_dlya_kompyutera/komplekt_8452471098",
        "priceDetailed": {"value": 5270},
    }
    item.update(extra)
    return item


def test_mfe_delivery_component_sets_delivery_flag() -> None:
    item = _item(
        iva={
            "GeoStep": [
                {
                    "componentData": {"component": "delivery"},
                    "payload": {"text": "Доставка от 1 дня"},
                }
            ]
        }
    )

    hits = parse_mfe_state(_mfe_html(item))

    assert len(hits) == 1
    assert hits[0].extra.get("delivery") is True


def test_mfe_without_delivery_component_keeps_delivery_unknown() -> None:
    hits = parse_mfe_state(_mfe_html(_item()))

    assert len(hits) == 1
    assert "delivery" not in hits[0].extra


def test_mfe_explicitly_unavailable_delivery_sets_false() -> None:
    item = _item(
        iva={
            "GeoStep": [
                {
                    "componentData": {"component": "delivery"},
                    "payload": {"text": "Доставка недоступна"},
                }
            ]
        }
    )

    hits = parse_mfe_state(_mfe_html(item))

    assert len(hits) == 1
    assert hits[0].extra.get("delivery") is False
