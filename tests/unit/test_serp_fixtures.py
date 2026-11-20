from __future__ import annotations

import gzip
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

import pytest

try:
    from hunter.collect.parsers.search import parse_search, price_missing_ratio
except ImportError:
    from hunter.collect.parsers.search import parse_search

    def price_missing_ratio(hits: list) -> float:
        missing = sum(1 for hit in hits if getattr(hit, "price", None) is None)
        return missing / len(hits) if hits else 0.0

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "html"
REPLAY_MANIFEST = Path(__file__).resolve().parents[1] / "fixtures" / "serp_replay_manifest.json"

# Known IDs from recommendation carousels in these fixtures (cvr-item) that must NOT be parsed:
CAROUSEL_SAMPLE_IDS = {
    "3905085500",  # Klipsch T5 in cvr-item
    "4623798497",  # B&W PX8 in cvr-item
    "8016607514",  # Sony XM5 in cvr-item
    "8048380996",  # Apple Pencil in cvr-item
    "8127506595",  # Bose QC in cvr-item
    "8341729137",  # B&W PX8 in cvr-item
    "8346190755",  # Huawei MatePad in cvr-item
    "8513140086",  # B&W PX6 in cvr-item
}


def _load_fixture(name: str) -> str:
    path = FIXTURES_DIR / name
    if path.suffix == ".gz":
        return gzip.decompress(path.read_bytes()).decode("utf-8", errors="replace")
    return path.read_text(encoding="utf-8")


def test_fixture_sony_xm6() -> None:
    html = _load_fixture("search_sony_xm6.html.gz")
    hits = parse_search(html)

    # 33 primary catalog grid items
    assert len(hits) == 33
    # All 33 have prices (missing ratio == 0)
    assert price_missing_ratio(hits) == 0.0
    for h in hits:
        assert h.price is not None
        assert h.avito_id not in CAROUSEL_SAMPLE_IDS


def test_fixture_bose_ultra() -> None:
    html = _load_fixture("search_bose_ultra.html.gz")
    hits = parse_search(html)

    # 4 primary catalog grid items
    assert len(hits) == 4
    assert price_missing_ratio(hits) == 0.0
    for h in hits:
        assert h.price is not None
        assert h.avito_id not in CAROUSEL_SAMPLE_IDS


def test_fixture_bw_px8() -> None:
    html = _load_fixture("search_bw_px8.html.gz")
    hits = parse_search(html)

    # 25 primary catalog grid items
    assert len(hits) == 25
    assert price_missing_ratio(hits) == 0.0
    for h in hits:
        assert h.price is not None
        assert h.avito_id not in CAROUSEL_SAMPLE_IDS


@pytest.mark.parametrize(
    "fixture_name,expected_min_hits",
    [
        ("search_sony_xm6.html.gz", 30),
        ("search_bose_ultra.html.gz", 4),
        ("search_bw_px8.html.gz", 20),
        ("short_bose_3items.html.gz", 3),
        ("short_cambridge_7items.html.gz", 6),
    ],
)
def test_all_hits_contract_validity(fixture_name: str, expected_min_hits: int) -> None:
    """S0 Contract: verify 100% of parsed hits conform to SearchHit invariants."""
    html = _load_fixture(fixture_name)
    hits = parse_search(html)
    assert len(hits) >= expected_min_hits
    for h in hits:
        assert h.avito_id and h.avito_id.isdigit()
        assert h.title and len(h.title.strip()) > 0
        assert h.url and ("avito.ru" in h.url or h.url.startswith("/"))
        assert h.price_state in ("ok", "missing", "suspect", "unspecified")
        if h.price is not None:
            assert isinstance(h.price, int) and h.price >= 0


def test_parser_explicit_empty_and_unknown_pages() -> None:
    """S0 Contract: check behavior on empty or malformed pages."""
    from hunter.collect.parsers.search import parse_search_page

    # Empty HTML
    hits, page_kind = parse_search_page("<html><body><div>Пусто</div></body></html>")
    assert len(hits) == 0
    assert page_kind == "unknown_empty"

    # Minimal blank
    hits2, kind2 = parse_search_page("")
    assert len(hits2) == 0
    assert kind2 == "unknown_empty"


def test_replay_manifest_hash_privacy_cache_and_parser_contracts() -> None:
    from hunter.application.metrics import classify_cache_freshness
    from hunter.collect.parsers.search import parse_search_page

    manifest_text = REPLAY_MANIFEST.read_text(encoding="utf-8")
    manifest = json.loads(manifest_text)
    assert manifest["schema_version"] == 1
    assert manifest["source_kind"] == "synthetic_safe_fixture"
    assert datetime.fromisoformat(manifest["created_at"].replace("Z", "+00:00"))

    fixture = manifest["fixtures"][0]
    relative_path = Path(fixture["path"])
    assert not relative_path.is_absolute() and ".." not in relative_path.parts
    raw = (FIXTURES_DIR / relative_path).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == fixture["sha256"]
    body = raw.decode("utf-8")
    pii_or_secret = (
        r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b",
        r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
        r"(?<!\d)(?:\+7|8)[\s().-]*\d(?:[\s().-]*\d){9,10}(?!\d)",
        r"(?i)\b(?:authorization|cookie|set-cookie)\s*:",
        r"(?i)https?://[^/\s:@]+:[^@\s]+@",
    )
    assert not any(re.search(pattern, body + manifest_text) for pattern in pii_or_secret)

    headers = fixture["headers"]
    assert set(headers) <= {"age", "cache-control", "content-type", "date"}
    hits = parse_search(body)
    actual_lots = [{"id": hit.avito_id, "price": hit.price} for hit in hits]
    assert actual_lots == fixture["expected_lots"]
    cache_state, _, _ = classify_cache_freshness(
        headers, datetime.fromisoformat(fixture["freshness_checked_at"].replace("Z", "+00:00"))
    )
    assert cache_state == fixture["expected_cache_state"]

    for case in manifest["page_states"]:
        parsed_hits, page_kind = parse_search_page(case["html"])
        assert not parsed_hits
        assert page_kind == case["expected"]

