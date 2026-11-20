#!/usr/bin/env python3
"""Replay script for PR-0b-3:
1. Replay 30 gem lots: table 'lot -> G0 / brand gate / prefilter / triage -> result'
2. Replay 450 lots: vision calls saved vs target lots cut (expect 0)
"""

import sqlite3
import yaml
from pathlib import Path
from hunter.core.models import SearchHit, WatchItem, Card
from hunter.core.config import get_settings
from hunter.pipeline.round1 import evaluate_stub, evaluate_card

GEM_IDS = [
    "8268397751", "8268538304", "8282936867", "8283217964", "8283322300",
    "8283735775", "8283826923", "8283880185", "8283934613", "8284038351",
    "8284055859", "8295912314", "8296372604", "8296392277", "8296436808",
    "8300197115", "8300820576", "8304528250", "8304924102", "8305792471",
    "8307909172", "8308064174", "8308234572", "8308248473", "8308409402",
    "8315106029", "8315255974", "8316085159", "8316839859", "8318138079",
]

def load_watches() -> dict[str, WatchItem]:
    with open("config/watchlists/gems.yaml", encoding="utf-8") as f:
        gems = yaml.safe_load(f)
    with open("config/watchlists/headphones.yaml", encoding="utf-8") as f:
        hps = yaml.safe_load(f)
    res = {}
    for item in gems + hps:
        res[item["id"]] = WatchItem(**item)
    return res

def run_30_gem_replay():
    watches = load_watches()
    conn = sqlite3.connect("file:data/prod_snapshot.db?mode=ro", uri=True)
    cur = conn.cursor()
    placeholders = ",".join("?" for _ in GEM_IDS)
    cur.execute(
        f"SELECT avito_id, watchlist_id, price, title FROM listings WHERE avito_id IN ({placeholders})",
        GEM_IDS,
    )
    rows = {r[0]: {"watch_id": r[1], "price": r[2], "title": r[3]} for r in cur.fetchall()}
    conn.close()

    print("=" * 115)
    print(f"{'№':<3} | {'Avito ID':<10} | {'Watchlist':<24} | {'Цена':<7} | {'Заголовок':<40} | {'Исход':<20}")
    print("=" * 115)

    g0_count = 0
    brand_count = 0
    prefilter_count = 0
    passed_to_vision = 0

    for idx, aid in enumerate(GEM_IDS, 1):
        info = rows.get(aid, {"watch_id": "gem_headphones_sony", "price": 0, "title": "Unknown"})
        w = watches.get(info["watch_id"]) or watches["gem_headphones_sony"]
        hit = SearchHit(
            avito_id=aid,
            url=f"https://avito.ru/{aid}",
            title=info["title"],
            price=info["price"],
        )
        dec = evaluate_stub(hit, w)
        short_title = (info["title"][:37] + "...") if len(info["title"]) > 40 else info["title"]

        if not dec.pass_stub:
            res_str = f"SKIP ({dec.skip_reason})"
            if dec.skip_reason == "title_other_model":
                g0_count += 1
            elif dec.skip_reason == "title_other_brand":
                brand_count += 1
            elif dec.skip_reason == "gem_prefilter":
                prefilter_count += 1
        else:
            res_str = "PASS -> Vision Triage"
            passed_to_vision += 1

        print(f"{idx:<3} | {aid:<10} | {w.id:<24} | {info['price']:<7} | {short_title:<40} | {res_str:<20}")

    print("=" * 115)
    print("30 GEM LOTS REPLAY SUMMARY:")
    print(f"Total lots: {len(GEM_IDS)}")
    print(f"Cut by G0 (title_other_model): {g0_count}")
    print(f"Cut by Brand Gate (title_other_brand): {brand_count}")
    print(f"Cut by Prefilter Stopwords (gem_prefilter): {prefilter_count}")
    print(f"Passed to Vision Triage: {passed_to_vision}")
    print("=" * 115)


def run_450_lots_replay():
    watches = load_watches()
    conn = sqlite3.connect("file:data/prod_snapshot.db?mode=ro", uri=True)
    cur = conn.cursor()
    cur.execute("SELECT avito_id, watchlist_id, price, title, description FROM listings")
    rows = cur.fetchall()
    conn.close()

    print(f"\nRunning replay on {len(rows)} lots from snapshot...")
    # Replay on all lots
    gem_lots = [r for r in rows if r[1] and r[1].startswith("gem_")]
    target_lots = [r for r in rows if r[1] and not r[1].startswith("gem_")]

    print(f"Gem feed lots: {len(gem_lots)}, Targeted watch lots: {len(target_lots)}")

    # On gem lots: how many cut by brand gate + prefilter
    gem_prefilter_cut = 0
    gem_brand_cut = 0
    gem_g0_cut = 0
    gem_passed = 0

    for aid, wid, price, title, _ in gem_lots:
        w = watches.get(wid)
        if not w:
            continue
        hit = SearchHit(avito_id=aid, url=f"https://avito.ru/{aid}", title=title, price=price)
        dec = evaluate_stub(hit, w)
        if not dec.pass_stub:
            if dec.skip_reason == "gem_prefilter":
                gem_prefilter_cut += 1
            elif dec.skip_reason == "title_other_brand":
                gem_brand_cut += 1
            elif dec.skip_reason == "title_other_model":
                gem_g0_cut += 1
        else:
            gem_passed += 1

    # On target lots: verify that 0 target lots are cut by gem prefilter / brand gate!
    target_falsely_cut = 0
    for aid, wid, price, title, _ in target_lots:
        w = watches.get(wid)
        if not w:
            continue
        hit = SearchHit(avito_id=aid, url=f"https://avito.ru/{aid}", title=title, price=price)
        dec = evaluate_stub(hit, w)
        if dec.skip_reason in ("gem_prefilter", "title_other_brand"):
            target_falsely_cut += 1

    print("=" * 60)
    print("450+ LOTS REPLAY RESULTS:")
    print(f"Gem lots evaluated: {len(gem_lots)}")
    print(f"  Cut by G0 (other model): {gem_g0_cut}")
    print(f"  Cut by Brand Gate (other brand): {gem_brand_cut}")
    print(f"  Cut by Prefilter Stopwords: {gem_prefilter_cut}")
    print(f"  Vision calls saved by prefilter & brand gate: {gem_prefilter_cut + gem_brand_cut}")
    print(f"  Remaining candidates for vision: {gem_passed}")
    print(f"Targeted lots falsely cut by gem filters: {target_falsely_cut} (EXPECTED: 0)")
    print("=" * 60)


if __name__ == "__main__":
    run_30_gem_replay()
    run_450_lots_replay()
