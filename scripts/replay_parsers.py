"""Скрипт реплея парсеров поисковой выдачи Avito.

Сравнивает результаты работы базового парсера (из коммита baseline-2026-10-04)
и обновленного парсера PR-0a на сохраненных HTML-снапшотах выдачи (*_search.html.gz).
Выявляет расхождения в количестве извлеченных карточек, ценах, фильтрации каруселей cvr-item
и гарантирует отсутствие потерь лотов с ценой (recall).
"""

from __future__ import annotations

import glob
import gzip
import hashlib
import subprocess
import sys
import types
from pathlib import Path

from selectolax.lexbor import LexborHTMLParser

from hunter.collect.parsers.search import (
    _is_carousel_or_recommendation,
    parse_search as parse_search_new,
)
from hunter.core.config import load_watchlists


def main() -> None:
    # Directory to scan for search html.gz snapshots
    raw_dir = Path("data/raw")
    if len(sys.argv) > 1:
        if sys.argv[1] == "--raw-dir" and len(sys.argv) > 2:
            raw_dir = Path(sys.argv[2])
        else:
            raw_dir = Path(sys.argv[1])

    # 1. Build URL hash -> watchlist_id mapping
    url_map: dict[str, str] = {}
    for w in load_watchlists():
        for u in w.search_urls:
            h = hashlib.sha256(u.encode()).hexdigest()[:12]
            url_map[h] = w.id

    # 2. Dynamically load baseline search parser from file or git baseline-2026-10-04 tag
    baseline_file = Path("baseline_search.py")
    if baseline_file.exists():
        baseline_code = baseline_file.read_text(encoding="utf-8")
    else:
        try:
            baseline_code = subprocess.check_output(
                ["git", "show", "baseline-2026-10-04:src/hunter/collect/parsers/search.py"],
                text=True,
            )
        except Exception:
            prod_search = Path("/home/egor/scraper/avito-hunter/src/hunter/collect/parsers/search.py")
            if prod_search.exists():
                baseline_code = prod_search.read_text(encoding="utf-8")
            else:
                raise

    baseline_mod = types.ModuleType("baseline_search")
    exec(baseline_code, baseline_mod.__dict__)
    parse_search_old = baseline_mod.parse_search

    # 3. Process all *_search.html.gz
    files = sorted(raw_dir.glob("*_search.html.gz"))
    if not files:
        print(f"No *_search.html.gz files found in {raw_dir}.")
        return

    print("=" * 115)
    header = (
        f"{'File':<30} | {'Watch ID':<26} | {'Old':<4} | {'New':<4} | "
        f"{'P_Old':<5} | {'P_New':<5} | {'cvr-item':<8} | {'Dropped_Priced':<14}"
    )
    print(header)
    print("-" * 115)

    all_dropped: list[dict] = []
    total_old = 0
    total_new = 0
    total_p_old = 0
    total_p_new = 0

    for file_path_str in files:
        p = Path(file_path_str)
        file_hash = p.name.split("_")[0]
        watch_id = url_map.get(file_hash, "unknown")

        with gzip.open(p, "rt", encoding="utf-8", errors="replace") as f:
            html = f.read()

        tree = LexborHTMLParser(html)
        cvr_nodes = tree.css(".cvr-item, [class*='cvr-item']")
        cvr_count = len(cvr_nodes)

        hits_old = parse_search_old(html)
        hits_new = parse_search_new(html)

        p_old = [h for h in hits_old if h.price is not None]
        p_new = [h for h in hits_new if h.price is not None]

        total_old += len(hits_old)
        total_new += len(hits_new)
        total_p_old += len(p_old)
        total_p_new += len(p_new)

        old_priced_map = {h.avito_id: h for h in p_old}
        new_ids = {h.avito_id for h in hits_new}

        dropped = [h for aid, h in old_priced_map.items() if aid not in new_ids]
        for h in dropped:
            # Check if element is in carousel in DOM
            node = tree.css_first(f"a[href*='{h.avito_id}']")
            in_carousel = _is_carousel_or_recommendation(node) if node else False
            all_dropped.append(
                {
                    "file": p.name,
                    "avito_id": h.avito_id,
                    "title": h.title,
                    "price": h.price,
                    "in_carousel": in_carousel,
                }
            )

        print(
            f"{p.name:<30} | {watch_id:<26} | {len(hits_old):<4} | {len(hits_new):<4} | "
            f"{len(p_old):<5} | {len(p_new):<5} | {cvr_count:<8} | {len(dropped):<14}"
        )

    print("-" * 115)
    print(
        f"{'TOTAL (' + str(len(files)) + ' files)':<30} | {'':<26} | "
        f"{total_old:<4} | {total_new:<4} | {total_p_old:<5} | {total_p_new:<5} | "
        f"{'':<8} | {len(all_dropped):<14}"
    )
    print("=" * 115)

    print("\n[Сводка по пропавшим лотам с ценой]")
    if not all_dropped:
        print("  -> Пропавших лотов с ценой: 0 (все валидные лоты с ценой сохранены в новом парсере).")
    else:
        for item in all_dropped:
            print(
                f"  - ID: {item['avito_id']}, File: {item['file']}, "
                f"Title: {item['title']}, Price: {item['price']}, В карусели: {item['in_carousel']}"
            )

    print("\n[Проверка каруселей в фикстурах tests/fixtures/html/]")
    fixture_files = [
        "search_sony_xm6.html.gz",
        "search_bose_ultra.html.gz",
        "search_bw_px8.html.gz",
    ]
    fixtures_dir = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "html"
    for fix_name in fixture_files:
        fix_path = fixtures_dir / fix_name
        if not fix_path.exists():
            print(f"  - {fix_name}: файл не найден!")
            continue
        with gzip.open(fix_path, "rt", encoding="utf-8", errors="replace") as f:
            fix_html = f.read()
        fix_tree = LexborHTMLParser(fix_html)
        containers = fix_tree.css(".cvr-items-container, [data-marker*='carousel'], [class*='carousel']")
        items = fix_tree.css(".cvr-item, [class*='cvr-item']")
        print(f"  - {fix_name}: контейнеров каруселей={len(containers)}, узлов cvr-item={len(items)}")


if __name__ == "__main__":
    main()
