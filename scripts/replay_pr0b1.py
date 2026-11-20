#!/usr/bin/env python3
"""Offline replay of PR-0b-1 rules, Gate G0, and Confirmation Gate on prod DB snapshot."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

from hunter.categories.registry import get_catalog
from hunter.core.config import get_settings, load_watchlists
from hunter.core.models import Card, SearchHit, WatchItem
from hunter.extract.gems_filter import is_obvious_junk
from hunter.extract.catalog_match import match_models
from hunter.pipeline.round1 import evaluate_card, evaluate_stub


def run_replay(db_path: Path) -> dict[str, Any]:
    # Force notify_mode to shadow for replay evaluation
    settings = get_settings()
    settings.notify_mode = "shadow"

    watches = {w.id: w for w in load_watchlists()}
    catalog = get_catalog()

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute(
        """
        SELECT avito_id, url, title, price, description, params, city, seller_type, watchlist_id, status
        FROM listings
        ORDER BY avito_id
        """
    )
    rows = cur.fetchall()
    conn.close()

    golden_fps = {
        "8346263839", "8419823500", "8448204528", "8362043941",
        "8196402239", "8102386132", "8019805985", "7505123236",
        "8283735775", "8337222121", "8399779076",
    }

    stats_by_watch: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "total": 0,
        "g0_skipped": 0,
        "other_stub_skipped": 0,
        "gem_junk_skipped": 0,
        "round1_passed": 0,
        "round1_skipped": 0,
        "skip_reasons": defaultdict(int),
    })

    would_send: list[dict[str, Any]] = []
    golden_results: dict[str, dict[str, Any]] = {}

    for row in rows:
        avito_id = row["avito_id"]
        watch_id = row["watchlist_id"]
        watch = watches.get(watch_id)
        if not watch:
            continue

        st = stats_by_watch[watch_id]
        st["total"] += 1

        hit = SearchHit(
            avito_id=avito_id,
            url=row["url"],
            title=row["title"],
            price=row["price"],
            city=row["city"],
            seller_type=row["seller_type"],
            snippet=row["description"],
        )

        # 1. Gate G0 / Stub
        stub_dec = evaluate_stub(hit, watch)
        if not stub_dec.pass_stub:
            st["skip_reasons"][stub_dec.skip_reason or "unknown_stub"] += 1
            if stub_dec.skip_reason == "title_other_model":
                st["g0_skipped"] += 1
            else:
                st["other_stub_skipped"] += 1

            if avito_id in golden_fps:
                golden_results[avito_id] = {
                    "title": row["title"],
                    "passed": False,
                    "reason": stub_dec.skip_reason,
                    "stage": "stub/gate_g0",
                }
            continue

        # 2. Gem junk filter & gem gate G0
        if watch.is_gem_feed:
            if is_obvious_junk(f"{row['title'] or ''} {row['description'] or ''}"):
                st["gem_junk_skipped"] += 1
                st["skip_reasons"]["gem_junk"] += 1
                if avito_id in golden_fps:
                    golden_results[avito_id] = {
                        "title": row["title"],
                        "passed": False,
                        "reason": "gem_junk",
                        "stage": "gem_filter",
                    }
                continue

            if row["title"]:
                cat_models = [
                    m for m, spec in catalog.models.items()
                    if not spec.get("category") or spec.get("category") == watch.category
                ]
                allowed_canonical = {catalog.canonical_id(m) for m in watch.models}
                t_match = match_models(row["title"], catalog, category=watch.category, allowed=cat_models)
                if t_match.model_id and watch.models and t_match.model_id not in allowed_canonical:
                    st["g0_skipped"] += 1
                    st["skip_reasons"]["title_other_model"] += 1
                    if avito_id in golden_fps:
                        golden_results[avito_id] = {
                            "title": row["title"],
                            "passed": False,
                            "reason": "title_other_model",
                            "stage": "gem_gate_g0",
                        }
                    continue

        # 3. Card evaluation & Confirmation Gate
        params = {}
        if row["params"]:
            try:
                params = json.loads(row["params"])
            except Exception:
                pass

        card = Card(
            avito_id=avito_id,
            url=row["url"],
            title=row["title"],
            price=row["price"],
            description=row["description"],
            params=params,
            city=row["city"],
            seller_type=row["seller_type"],
        )

        card_dec = evaluate_card(card, watch, snapshot=None)
        if not card_dec.pass_stub or card_dec.verdict in ("skip", None):
            st["round1_skipped"] += 1
            st["skip_reasons"][card_dec.skip_reason or "round1_skip"] += 1
            if avito_id in golden_fps:
                golden_results[avito_id] = {
                    "title": row["title"],
                    "passed": False,
                    "reason": card_dec.skip_reason,
                    "stage": "round1/confirmation_gate",
                }
            continue

        # Passed!
        st["round1_passed"] += 1
        model_id = card_dec.extraction.model_id if card_dec.extraction else "unknown"
        item = {
            "avito_id": avito_id,
            "title": row["title"],
            "price": row["price"],
            "watch": watch_id,
            "model": model_id,
            "verdict": card_dec.verdict,
            "reasons": ", ".join(card_dec.reasons),
        }
        would_send.append(item)

        if avito_id in golden_fps:
            golden_results[avito_id] = {
                "title": row["title"],
                "passed": True,
                "reason": "PASSED_UNEXPECTEDLY",
                "stage": "none",
            }

    return {
        "stats_by_watch": dict(stats_by_watch),
        "would_send": would_send,
        "golden_results": golden_results,
    }


def main():
    db_path = Path("data/prod_snapshot.db")
    if not db_path.exists():
        print(f"Error: {db_path} does not exist")
        return

    results = run_replay(db_path)

    print("================ REPLAY SUMMARY BY WATCHLIST ================")
    print(f"{'Watchlist':<36} | {'Total':<6} | {'G0 Skip':<8} | {'Stub/Other':<10} | {'Round1 Pass':<12}")
    print("-" * 85)
    for watch_id, st in results["stats_by_watch"].items():
        print(f"{watch_id:<36} | {st['total']:<6} | {st['g0_skipped']:<8} | {st['other_stub_skipped'] + st['gem_junk_skipped'] + st['round1_skipped']:<10} | {st['round1_passed']:<12}")

    print("\n================ GOLDEN FALSE POSITIVES CHECK ================")
    all_golden_rejected = True
    for avito_id, res in results["golden_results"].items():
        passed = res["passed"]
        if passed:
            all_golden_rejected = False
        status_str = "FAILED (PASSED)" if passed else "PASSED (REJECTED)"
        print(f"ID {avito_id}: {status_str} via {res['stage']} ({res['reason']}) - '{res['title']}'")

    print(f"\nAll Golden False Positives Rejected: {all_golden_rejected}")

    print(f"\n================ WOULD-SEND LIST ({len(results['would_send'])} items) ================")
    for i, item in enumerate(results["would_send"][:60], 1):
        print(f"{i:2d}. [{item['watch']}] {item['model']} | {item['price']} руб. | '{item['title']}' | {item['verdict']} | {item['reasons']}")


if __name__ == "__main__":
    main()
