"""First set of pure decision evaluators: parsing, pricing, matching, round1."""

from __future__ import annotations

from typing import Any

from hunter.collect.parsers.mfe_state import parse_mfe_state
from hunter.collect.parsers.search import parse_price, parse_search_page
from hunter.core.models import Card, Extraction, Round1Decision, SearchHit, WatchItem
from hunter.core.product import ProductStore
from hunter.matching.engine import evaluate_product_match, match_token_in_text
from hunter.pipeline.round1 import evaluate_card, evaluate_stub
from hunter.pricing.market import snapshot_from_prices
from hunter.pricing.ml import compute_market_quantiles, estimate_fair_price
from hunter.pricing.value import laptop_value_score
from tools.parity.common import load_fixture_text


def evaluate_serp_parsing() -> dict[str, Any]:
    """Evaluate pure search results parsing on fixture HTML pages."""
    fixture_files = [
        "tests/fixtures/html/search_sony_xm6.html.gz",
        "tests/fixtures/html/search_bose_ultra.html.gz",
        "tests/fixtures/html/search_bw_px8.html.gz",
        "tests/fixtures/html/short_bose_3items.html.gz",
        "tests/fixtures/html/short_cambridge_7items.html.gz",
    ]
    results: dict[str, Any] = {}
    for fix in fixture_files:
        html = load_fixture_text(fix)
        hits, stats = parse_search_page(html)
        mfe_hits = parse_mfe_state(html) or []
        results[fix] = {
            "stats": stats,
            "hits_count": len(hits),
            "sample_hits": [
                {
                    "avito_id": h.avito_id,
                    "title": h.title,
                    "price": h.price,
                    "old_price": h.old_price,
                    "discount_pct": h.discount_pct,
                }
                for h in hits[:5]
            ],
            "mfe_hits_count": len(mfe_hits),
        }
    return results


def evaluate_pricing_valuation() -> dict[str, Any]:
    """Evaluate pure pricing parsing, market percentiles, and laptop scoring."""
    sample_texts = [
        " 25 000 ₽ ",
        "1 234 567 руб.",
        "от 500",
        "Бесплатно",
        "10.500",
        "договорная",
    ]
    parsed_prices = {text: parse_price(text) for text in sample_texts}

    distributions = {
        "headphones": [15000, 18000, 20000, 22000, 25000, 30000, 35000],
        "uniform": [5000, 5000, 5000, 5000],
        "laptops": [45000, 55000, 70000, 85000, 110000],
    }
    snaps = {k: snapshot_from_prices(v) for k, v in distributions.items()}
    quantiles = {k: compute_market_quantiles(v) for k, v in distributions.items()}

    ext = Extraction(
        model_id="sony_wh_1000xm6",
        specs={},
        condition="used",
        defects=[],
        confidence=0.9,
    )
    fair_estimate = estimate_fair_price(21000, extraction=ext, snapshot=snaps["headphones"])

    laptop_tests = [
        ("i5-1235U", 35000),
        ("i5-1335U", 45000),
        ("i7-1255U", 55000),
        ("unknown-cpu", 30000),
    ]
    laptop_scores = {f"{c}_{p}": laptop_value_score(c, p) for c, p in laptop_tests}

    return {
        "parsed_prices": parsed_prices,
        "snapshots": snaps,
        "quantiles": quantiles,
        "fair_estimate": fair_estimate,
        "laptop_scores": laptop_scores,
    }


def evaluate_model_matching() -> dict[str, Any]:
    """Evaluate pure model matching with aliases, homoglyphs, and form factors."""
    from tests.fixtures.arch_state import get_synthetic_parity_profiles

    profiles = get_synthetic_parity_profiles()
    sony = profiles["gem_headphones_sony"]
    bose = profiles["bose_quietcomfort_ultra_gen_2"]

    test_titles = [
        "Sony WH-1000XM6 Black в Москве",
        "Sony WH-1000XM5 в идеале",
        "Наушники Sony WH 1000 XM6 черные",
        "Чехол для Sony WH-1000XM6",
        "Sony WH-1000XM6 копия / реплика",
        "Наушники Сони ХМ6 новые",
        "Sony WF-1000XM6 внутриканальные",
        "Bose QuietComfort Ultra Headphones",
        "Bose QuietComfort Ultra Earbuds gen 2",
        "Чехол Bose QuietComfort Ultra",
    ]

    verdicts: dict[str, Any] = {}
    for title in test_titles:
        v_sony = evaluate_product_match(title, sony)
        v_bose = evaluate_product_match(title, bose)
        verdicts[title] = {
            "sony": {
                "passed": v_sony.passed,
                "score": v_sony.score,
                "reason": v_sony.reason,
                "matched_alias": v_sony.matched_alias,
            },
            "bose": {
                "passed": v_bose.passed,
                "score": v_bose.score,
                "reason": v_bose.reason,
                "matched_alias": v_bose.matched_alias,
            },
        }

    tokens = [
        ("хм6", "Sony WH-1000XM6"),
        ("хм5", "Sony WH-1000XM5"),
        ("airpods", "Apple Airpods Pro 2"),
    ]
    token_checks = {f"{tok}_{text}": match_token_in_text(tok, text) for tok, text in tokens}

    return {"verdicts": verdicts, "token_checks": token_checks}


def evaluate_stage0_round1() -> dict[str, Any]:
    """Evaluate pure round 1 fast filter decisions."""
    watch = WatchItem(
        id="sony_xm6",
        category="headphones",
        models=["Sony WH-1000XM6"],
        max_price=30000,
        search_urls=["https://avito.ru/moskva/audio_i_video"],
    )

    hits = [
        SearchHit(avito_id="101", title="Sony WH-1000XM6 новые", price=25000, url="https://avito.ru/101"),
        SearchHit(avito_id="102", title="Чехол для Sony WH-1000XM6", price=25000, url="https://avito.ru/102"),
        SearchHit(avito_id="103", title="Sony WH-1000XM6", price=5000, url="https://avito.ru/103"),
        SearchHit(avito_id="104", title="Sony WH-1000XM6 оригинал", price=45000, url="https://avito.ru/104"),
    ]

    stub_decisions: dict[str, Any] = {}
    for h in hits:
        dec = evaluate_stub(h, watch)
        stub_decisions[h.avito_id] = {
            "pass_stub": dec.pass_stub,
            "skip_reason": dec.skip_reason,
            "reasons": dec.reasons,
            "verdict": dec.verdict,
        }

    card = Card(
        avito_id="201",
        title="Sony WH-1000XM6 в коробке",
        price=27000,
        url="https://avito.ru/201",
        description="Куплены в Финляндии, чек, коробка.",
        params={"Состояние": "Б/у"},
    )
    card_dec = evaluate_card(
        card,
        watch,
        snapshot={"median": 29000, "q10": 24000, "q25": 26000, "q75": 32000, "q90": 34000},
    )

    return {
        "stub_decisions": stub_decisions,
        "card_decision": {
            "pass_stub": card_dec.pass_stub,
            "skip_reason": card_dec.skip_reason,
            "reasons": card_dec.reasons,
            "verdict": card_dec.verdict,
        },
    }
