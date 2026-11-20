#!/usr/bin/env python3
"""Run gem vision triage on 30 saved gem lots with images."""

import asyncio
import sqlite3
from pathlib import Path
import yaml
from hunter.vision.gem_triage import triage_images_for_gem

GEM_IDS = [
    "8268397751", "8268538304", "8282936867", "8283217964", "8283322300",
    "8283735775", "8283826923", "8283880185", "8283934613", "8284038351",
    "8284055859", "8295912314", "8296372604", "8296392277", "8296436808",
    "8300197115", "8300820576", "8304528250", "8304924102", "8305792471",
    "8307909172", "8308064174", "8308234572", "8308248473", "8308409402",
    "8315106029", "8315255974", "8316085159", "8316839859", "8318138079",
]


def load_watch_models() -> dict[str, list[str]]:
    with open("config/watchlists/gems.yaml", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    mapping = {}
    for item in data:
        mapping[item["id"]] = item.get("models", [])
    return mapping


async def main():
    watch_models = load_watch_models()
    conn = sqlite3.connect("file:data/prod_snapshot.db?mode=ro", uri=True)
    cur = conn.cursor()
    placeholders = ",".join("?" for _ in GEM_IDS)
    cur.execute(
        f"SELECT avito_id, watchlist_id, price, title FROM listings WHERE avito_id IN ({placeholders})",
        GEM_IDS,
    )
    listings_by_id = {row[0]: {"watch_id": row[1], "price": row[2], "title": row[3]} for row in cur.fetchall()}
    conn.close()

    print(f"Starting vision triage on {len(GEM_IDS)} saved gem lots...")
    confirmed = []
    not_confirmed = []
    unavailable = []

    for idx, aid in enumerate(GEM_IDS, 1):
        lot_info = listings_by_id.get(aid, {"watch_id": "gem_headphones_sony", "price": 0, "title": "Unknown"})
        watch_id = lot_info["watch_id"]
        models = watch_models.get(watch_id, ["sony_wh1000xm6"])
        img_dir = Path("data/images") / aid
        imgs = sorted([p for p in img_dir.glob("img_*.jpg") if p.is_file() and p.stat().st_size > 0])
        if not imgs:
            imgs = sorted([p for p in img_dir.glob("*.jpg") if p.is_file() and p.stat().st_size > 0 and p.name != "collage.jpg"])

        if not imgs:
            print(f"[{idx}/30] {aid}: No valid images found in {img_dir}")
            unavailable.append((aid, lot_info, "No images found"))
            continue

        result = await triage_images_for_gem(imgs[:2], allowed_models=models)
        if not result.available:
            print(f"[{idx}/30] {aid}: Vision unavailable")
            unavailable.append((aid, lot_info, "Vision API unavailable"))
        elif result.is_gem_candidate:
            print(f"[{idx}/30] {aid}: CONFIRMED as {result.matched_target} (conf={result.confidence:.2f}): {lot_info['title']} ({lot_info['price']} RUB) - {result.reason}")
            confirmed.append((aid, lot_info, result))
        else:
            print(f"[{idx}/30] {aid}: REJECTED (target={result.matched_target}): {lot_info['title']} ({lot_info['price']} RUB) - {result.reason}")
            not_confirmed.append((aid, lot_info, result))

        # Respect rate limits between calls
        await asyncio.sleep(0.5)

    print("\n" + "=" * 60)
    print("TRIAGE SUMMARY:")
    print(f"Total processed: {len(GEM_IDS)}")
    print(f"Confirmed candidates: {len(confirmed)}")
    print(f"Rejected / Not confirmed: {len(not_confirmed)}")
    print(f"Unavailable: {len(unavailable)}")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
