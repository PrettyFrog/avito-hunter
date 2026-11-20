from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hunter.collect.card import fetch_card
from hunter.collect.parsers.card import parse_card, parse_card_mobile_json
from hunter.core.models import FetchResult


SAMPLE_MOBILE_CARD_JSON = {
    "success": {
        "mobile": {
            "id": 8413638386,
            "title": "Наушники bose",
            "price": {"value": "2\xa0000", "metric": "₽"},
            "description": "Наушники новые, не использовались.",
            "address": "д. Андреевка, Молодёжная ул.",
            "seller": {
                "userHash": "seller_hash_abc",
                "profileType": "person",
            },
            "images": [
                {
                    "1280x723": "https://10.img.avito.st/image/1/1.img1280.jpg",
                    "720x407": "https://10.img.avito.st/image/1/1.img720.jpg",
                },
                {
                    "1080x610": "https://10.img.avito.st/image/1/1.img2_1080.jpg",
                },
            ],
            "parameters": {
                "flat": [
                    {"title": "Производитель", "description": "BOSE"},
                    {"title": "Модель", "description": "QuietComfort"},
                    {"title": "Состояние", "description": "Б/у"},
                ]
            },
        }
    }
}


def test_parse_card_mobile_json():
    card = parse_card_mobile_json(SAMPLE_MOBILE_CARD_JSON, url="https://avito.ru/item/8413638386")
    assert card is not None
    assert card.avito_id == "8413638386"
    assert card.title == "Наушники bose"
    assert card.price == 2000
    assert card.description == "Наушники новые, не использовались."
    assert card.city == "д. Андреевка, Молодёжная ул."
    assert card.seller_id == "seller_hash_abc"
    assert card.seller_type == "person"
    assert len(card.image_urls) == 2
    assert card.image_urls[0] == "https://10.img.avito.st/image/1/1.img1280.jpg"
    assert card.params.get("Производитель") == "BOSE"
    assert card.params.get("Состояние") == "Б/у"


def test_parse_card_handles_json_string():
    raw_json = json.dumps(SAMPLE_MOBILE_CARD_JSON)
    card = parse_card(raw_json, url="https://avito.ru/item/8413638386")
    assert card is not None
    assert card.avito_id == "8413638386"
    assert card.title == "Наушники bose"
    assert card.price == 2000


def test_parse_card_handles_json_with_floats_and_ids():
    sample = {
        "success": {
            "mobile": {
                "id": 8327999555,
                "title": "iPhone 17, 256 ГБ, SIM + eSIM",
                "price": {"value": "62 990", "metric": "₽"},
                "description": "Телефон в около идеальном состоянии",
                "coords": {"lat": 51.8745932426, "lng": 55.16707761579317},
                "images": [{"1280x723": "https://avito.st/img1.jpg"}],
            }
        }
    }
    raw_json = json.dumps(sample)
    card = parse_card(raw_json, url="https://avito.ru/item/8327999555")
    assert card is not None
    assert card.avito_id == "8327999555"
    assert card.title == "iPhone 17, 256 ГБ, SIM + eSIM"
    assert card.price == 62990
    assert card.description == "Телефон в около идеальном состоянии"


@pytest.mark.asyncio
async def test_fetch_card_mobile_success(tmp_path: Path):
    mock_browser = MagicMock()
    mock_browser.fetch = AsyncMock()

    with patch("hunter.collect.card._fetch_card_mobile") as mock_mobile:
        from hunter.core.models import Card

        expected_card = Card(
            avito_id="8413638386",
            url="https://avito.ru/item/8413638386",
            title="Наушники bose",
            price=2000,
            description="Оригинал",
        )
        expected_fetched = FetchResult(
            url="https://m.avito.ru/api/1/card/items/8413638386",
            status=200,
            html="{}",
            latency_ms=120,
        )
        mock_mobile.return_value = (expected_fetched, expected_card, tmp_path / "raw.json")

        fetched, card, path = await fetch_card(
            mock_browser, "https://avito.ru/item/8413638386", "8413638386"
        )
        assert card is expected_card
        assert fetched.status == 200
        # Browser fetch should NOT have been called because mobile succeeded
        assert mock_browser.fetch.call_count == 0


@pytest.mark.asyncio
async def test_fetch_card_fallback_to_browser(tmp_path: Path):
    mock_browser = MagicMock()
    from hunter.core.models import FetchResult

    mock_browser.fetch = AsyncMock(
        return_value=FetchResult(
            url="https://avito.ru/item/999",
            status=200,
            html="<html><body><h1>Test</h1></body></html>",
        )
    )

    with patch("hunter.collect.card._fetch_card_mobile", return_value=None):
        fetched, card, path = await fetch_card(mock_browser, "https://avito.ru/item/999", "999")
        assert fetched.status == 200
        # Browser fetch was invoked on fallback
        assert mock_browser.fetch.call_count == 1
