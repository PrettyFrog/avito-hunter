from __future__ import annotations

import gzip
from pathlib import Path

from hunter.collect.parsers.card import parse_card

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_card_parser_extracts_real_gallery_images():
    card_gz = REPO_ROOT / "data" / "8413638386_card.html.gz"
    if not card_gz.exists():
        return

    with gzip.open(card_gz, "rt", encoding="utf-8") as f:
        html = f.read()

    card = parse_card(
        html,
        url="https://www.avito.ru/temnikov/audio_i_video/naushniki_bose_8413638386",
    )
    assert card is not None
    assert card.avito_id == "8413638386"
    assert card.title == "Наушники bose"
    assert card.price == 2000

    # Must extract genuine gallery photos, not stub icons
    assert len(card.image_urls) == 3
    for url in card.image_urls:
        assert "img.avito.st/image/" in url
        assert "touch-icon" not in url
        assert "holiday-logo" not in url
        assert "stub_avatars" not in url
