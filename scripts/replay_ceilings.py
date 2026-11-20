"""Replay script for Task F: Price ceiling recalculation (x1.0, x1.2, x1.5)."""

import sqlite3
import sys
from hunter.core.config import load_watchlists
from hunter.core.models import Card, WatchItem
from hunter.extract.rules import extract_rules
from hunter.pipeline.round1 import evaluate_stub, evaluate_card

db_path = sys.argv[1] if len(sys.argv) > 1 else "data/hunter.db"
con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
cur = con.cursor()
cur.execute(
    "SELECT avito_id, title, price, city, seller_type, watchlist_id, description "
    "FROM listings WHERE price IS NOT NULL"
)
rows = cur.fetchall()
con.close()

watches = load_watchlists()
watch_map = {w.id: w for w in watches}

multipliers = [1.0, 1.2, 1.5]
results = {w.id: {m: [] for m in multipliers} for w in watches}

# Reference prices for anchor calculation
ref_prices = {
    "sony_xm6": 36000,
    "bose_quietcomfort_ultra_gen_2": 35000,
    "Bowers_wilkins_px8_s2": 56000,
    "Bowers_wilkins_px7_s3": 32000,
    "cambridge_audio_melomania_p100_se": 24000,
    "gem_headphones_sony": 36000,
    "gem_headphones_bose": 35000,
    "gem_headphones_bowers": 56000,
    "gem_headphones_cambridge": 24000,
}

for aid, title, price, city, seller_type, wid, desc in rows:
    for w in watches:
        # Check if listing is relevant to this watch
        if wid and wid != w.id:
            continue

        desc_str = desc or ""
        card = Card(
            avito_id=aid,
            title=title or "",
            price=price,
            city=city,
            seller_type=seller_type,
            category=w.category,
            url=f"https://www.avito.ru/{aid}",
            description=desc_str,
        )

        extraction = extract_rules(
            title=card.title,
            description=card.description,
            params=card.params,
            category=w.category,
            allowed_models=w.models,
        )

        # For model watches: must match model in title
        if w.models and not w.is_gem_feed:
            if extraction.model_id not in w.models:
                continue

        # For gem feeds: check obvious junk
        if w.is_gem_feed:
            from hunter.extract.gems_filter import is_obvious_junk
            if is_obvious_junk(f"{card.title} {desc_str}"):
                continue
            if not extraction.model_id:
                continue
            if extraction.model_id not in (w.models or []):
                continue

        for m in multipliers:
            ceiling = int(w.max_price * m)
            if price <= ceiling:
                test_watch = w.model_copy(update={"max_price": ceiling})
                decision = evaluate_card(card, test_watch, snapshot=None, extraction=extraction)
                if decision.pass_stub and decision.verdict in ("watch", "buy_now", "ask_questions"):
                    results[w.id][m].append((aid, card.title, price, extraction.model_id))

print("=== ТАБЛИЦА ПЕРЕСЧЁТА ПОТОЛКОВ ЦЕН (Task F) ===")
print(f"{'Вотч ID':35s} | {'Потолок x1.0':13s} | {'x1.0':5s} | {'Потолок x1.2':13s} | {'x1.2':5s} | {'Потолок x1.5':13s} | {'x1.5':5s}")
print("-" * 105)
for w in watches:
    c1 = f"{w.max_price} ₽"
    c12 = f"{int(w.max_price * 1.2)} ₽"
    c15 = f"{int(w.max_price * 1.5)} ₽"
    n1 = len(results[w.id][1.0])
    n12 = len(results[w.id][1.2])
    n15 = len(results[w.id][1.5])
    print(f"{w.id:35s} | {c1:13s} | {n1:5d} | {c12:13s} | {n12:5d} | {c15:13s} | {n15:5d}")

print("\n=== ЛОТЫ, ДОБАВЛЯЮЩИЕСЯ ПРИ x1.2 (title, price, цена от ориентира) ===")
total_added = 0
for w in watches:
    ids_1 = {r[0] for r in results[w.id][1.0]}
    added_12 = [r for r in results[w.id][1.2] if r[0] not in ids_1]
    if added_12:
        total_added += len(added_12)
        ref = ref_prices.get(w.id, w.max_price)
        print(f"\n--- Вотч: {w.id} (ориентир цены новой модели: {ref} ₽, добавлено при x1.2: {len(added_12)}) ---")
        for aid, title, price, _ in added_12:
            diff = price - ref
            pct = (price / ref) * 100
            diff_str = f"{diff:+d} ₽ ({pct:.1f}% от ориентира)"
            extra = ""
            if "xm6" in w.id:
                if price < 0.45 * ref:
                    extra = " [⚠ ПОДОЗРИТЕЛЬНО НИЗКАЯ ЦЕНА (<45%)]"
                else:
                    extra = " [OK: адекватная цена (>=45%)]"
            print(f"• {aid}: \"{title}\" — {price} ₽ ({diff_str}){extra}")

if total_added == 0:
    print("При повышении потолка на +20% (x1.2) новых подходящих лотов в текущей базе не появилось.")
