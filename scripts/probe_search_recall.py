"""Script to run the one-time search recall probe (strictly 10 requests) for PR-0b-9."""

import asyncio
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from hunter.categories.registry import get_catalog
from hunter.collect.browser import BrowserSession
from hunter.collect.parsers.search import parse_search
from hunter.core.config import get_settings, load_watchlists
from hunter.core.models import SearchHit, WatchItem
from hunter.pipeline.round1 import evaluate_stub

PROBE_CONFIG = [
    {
        "watch_id": "sony_xm6",
        "queries": [
            {
                "label": "сони хм6",
                "url": "https://www.avito.ru/all/bytovaya_elektronika?f=ASgCAgECAUXGmgwZeyJmcm9tIjo2MDAwLCJ0byI6MTkzMjB9&s=104&q=%D1%81%D0%BE%D0%BD%D0%B8+%D1%85%D0%BC6",
            },
            {
                "label": "xm6 sony",
                "url": "https://www.avito.ru/all/bytovaya_elektronika?f=ASgCAgECAUXGmgwZeyJmcm9tIjo2MDAwLCJ0byI6MTkzMjB9&s=104&q=xm6+sony",
            },
        ],
    },
    {
        "watch_id": "bose_quietcomfort_ultra_gen_2",
        "queries": [
            {
                "label": "bose qc ultra 2",
                "url": "https://www.avito.ru/all/audio_i_video/naushniki-ASgBAgICAUSIAtRO?f=ASgBAgECAUSIAtROAUXGmgwYeyJmcm9tIjo4MDAwLCJ0byI6MjE2MDB9&localPriority=0&s=104&q=bose+qc+ultra+2",
            },
            {
                "label": "bose ultra 2 gen",
                "url": "https://www.avito.ru/all/audio_i_video/naushniki-ASgBAgICAUSIAtRO?f=ASgBAgECAUSIAtROAUXGmgwYeyJmcm9tIjo4MDAwLCJ0byI6MjE2MDB9&localPriority=0&s=104&q=bose+ultra+2+gen",
            },
        ],
    },
    {
        "watch_id": "Bowers_wilkins_px8_s2",
        "queries": [
            {
                "label": "px8 s2",
                "url": "https://www.avito.ru/all/bytovaya_elektronika?f=ASgCAgECAUXGmgwZeyJmcm9tIjoxNTAwMCwidG8iOjQ4MDAwfQ&s=104&q=px8+s2",
            },
            {
                "label": "b&w px8",
                "url": "https://www.avito.ru/all/bytovaya_elektronika?f=ASgCAgECAUXGmgwZeyJmcm9tIjoxNTAwMCwidG8iOjQ4MDAwfQ&s=104&q=b%26w+px8",
            },
        ],
    },
    {
        "watch_id": "Bowers_wilkins_px7_s3",
        "queries": [
            {
                "label": "px7 s3",
                "url": "https://www.avito.ru/all/bytovaya_elektronika?f=ASgCAgECAUXGmgwYeyJmcm9tIjo5MDAwLCJ0byI6Mjc2MDB9&s=104&q=px7+s3",
            },
            {
                "label": "b&w px7 s3",
                "url": "https://www.avito.ru/all/bytovaya_elektronika?f=ASgCAgECAUXGmgwYeyJmcm9tIjo5MDAwLCJ0byI6Mjc2MDB9&s=104&q=b%26w+px7+s3",
            },
        ],
    },
    {
        "watch_id": "cambridge_audio_melomania_p100_se",
        "queries": [
            {
                "label": "p100 se",
                "url": "https://www.avito.ru/all/audio_i_video/naushniki-ASgBAgICAUSIAtRO?pmax=30000&s=104&q=p100+se",
            },
            {
                "label": "cambridge melomania p100",
                "url": "https://www.avito.ru/all/audio_i_video/naushniki-ASgBAgICAUSIAtRO?pmax=30000&s=104&q=cambridge+melomania+p100",
            },
        ],
    },
]


def load_seen_lots(db_path: Path, watch_id: str) -> set[str]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    cur = conn.cursor()
    # Seen by combat feed within 48 hours
    seen = set()
    cur.execute(
        """
        SELECT avito_id FROM listings
        WHERE watchlist_id = ?
          AND (last_seen >= datetime('now', '-48 hours') OR first_seen >= datetime('now', '-48 hours'))
        """,
        (watch_id,),
    )
    for row in cur.fetchall():
        seen.add(str(row[0]))

    # Also include near_miss events in last 48 hours
    cur.execute(
        """
        SELECT le.avito_id FROM listing_events le
        JOIN listings l ON le.avito_id = l.avito_id
        WHERE l.watchlist_id = ?
          AND le.kind = 'near_miss'
          AND le.ts >= datetime('now', '-48 hours')
        """,
        (watch_id,),
    )
    for row in cur.fetchall():
        seen.add(str(row[0]))

    conn.close()
    return seen


async def run_probe():
    settings = get_settings()
    db_path = settings.data_dir / "hunter.db"
    watches_by_id = {w.id: w for w in load_watchlists()}

    os.environ["DISPLAY"] = ":95"
    browser = BrowserSession()
    await browser.start()

    results = []
    total_requests = 0

    print("=== STARTING SEARCH RECALL PROBE (10 REQUESTS) ===")
    try:
        for item in PROBE_CONFIG:
            w_id = item["watch_id"]
            watch = watches_by_id[w_id]
            seen_48h = load_seen_lots(db_path, w_id)
            print(f"\nWatch: {w_id} (seen in 48h: {len(seen_48h)} lots, max_price: {watch.max_price})")

            watch_res = {
                "watch_id": w_id,
                "seen_48h_count": len(seen_48h),
                "queries": [],
            }

            for q_info in item["queries"]:
                total_requests += 1
                q_label = q_info["label"]
                url = q_info["url"]
                print(f"[{total_requests}/10] Requesting '{q_label}' for {w_id}...")

                # Polite delay between requests
                if total_requests > 1:
                    print("  Polite delay 12s...")
                    await asyncio.sleep(12)

                fetched = await browser.fetch(url)
                if fetched.captcha or fetched.blocked:
                    print(f"  [WARN] Blocked or captcha: status={fetched.status}")
                hits = parse_search(fetched.html, base_url="https://www.avito.ru")
                print(f"  Status: {fetched.status}, DOM hits found: {len(hits)}")

                query_res = {
                    "label": q_label,
                    "url": url,
                    "status": fetched.status,
                    "total_hits": len(hits),
                    "unseen_hits": [],
                    "passing_hits": [],
                }

                for h in hits:
                    is_unseen = h.avito_id not in seen_48h
                    if is_unseen:
                        stub = evaluate_stub(h, watch)
                        # Check title passes all gates and price <= watch.max_price
                        passes_title = stub.pass_stub or (stub.skip_reason == "near_miss")
                        passes_price = (h.price is not None) and (h.price <= watch.max_price)
                        full_pass = passes_title and passes_price

                        hit_data = {
                            "avito_id": h.avito_id,
                            "title": h.title,
                            "price": h.price,
                            "url": h.url,
                            "pass_stub": stub.pass_stub,
                            "verdict": stub.verdict,
                            "skip_reason": stub.skip_reason,
                            "reasons": stub.reasons,
                            "passes_title": passes_title,
                            "passes_price": passes_price,
                            "full_pass": full_pass,
                        }
                        query_res["unseen_hits"].append(hit_data)
                        if full_pass:
                            query_res["passing_hits"].append(hit_data)

                watch_res["queries"].append(query_res)

            results.append(watch_res)

    finally:
        print("\nClosing browser...")
        if browser.context:
            await browser.context.close()
        if browser._playwright:
            await browser._playwright.stop()

    # Save full JSON report
    out_file = Path("/home/egor/scraper/avito-hunter/data/probe_results.json")
    out_file.write_text(json.dumps(results, indent=2, ensure_ascii=False))
    print(f"\nFull probe results saved to {out_file}")

    # Summary table
    print("\n" + "=" * 90)
    print("SEARCH RECALL PROBE SUMMARY TABLE")
    print("=" * 90)
    print(f"{'Watchlist ID':<35} | {'Запрос':<22} | {'Найдено':<8} | {'Не виделись 48ч':<15} | {'Проходят всё':<12}")
    print("-" * 90)
    for wr in results:
        for qr in wr["queries"]:
            print(
                f"{wr['watch_id']:<35} | {qr['label']:<22} | {qr['total_hits']:<8} | "
                f"{len(qr['unseen_hits']):<15} | {len(qr['passing_hits']):<12}"
            )

    print("\n" + "=" * 90)
    print("LOTS PASSING ALL GATES (TITLE + PRICE <= MAX_PRICE):")
    print("=" * 90)
    found_any = False
    for wr in results:
        for qr in wr["queries"]:
            for ph in qr["passing_hits"]:
                found_any = True
                print(f"- Watch: {wr['watch_id']} | Query: '{qr['label']}'")
                print(f"  ID: {ph['avito_id']} | Price: {ph['price']} ₽ | Title: {ph['title']}")
                print(f"  URL: {ph['url']}")
    if not found_any:
        print("None. No unseen lots passed all gating criteria and max_price ceiling.")


if __name__ == "__main__":
    asyncio.run(run_probe())
