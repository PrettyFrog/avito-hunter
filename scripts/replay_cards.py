"""Replay card parsers: baseline vs new parser on data/raw/*_card.html.gz."""

from __future__ import annotations

import glob
import gzip
import re
import sys
from pathlib import Path
from selectolax.lexbor import LexborHTMLParser

from hunter.collect.parsers.card import parse_card as parse_card_new
from hunter.collect.parsers.search import _coerce_price, _json_blobs, _walk_items, extract_avito_id
from hunter.core.models import Card
from hunter.core.rawstore import scrub_text


def parse_card_baseline(html: str, *, url: str) -> Card | None:
    html = scrub_text(html)
    avito_id = extract_avito_id(url)
    tree = LexborHTMLParser(html)
    data: dict = {}
    for blob in _json_blobs(html):
        for item in _walk_items(blob):
            ident = str(item.get("id") or item.get("itemId") or "")
            if avito_id and ident and ident != avito_id:
                continue
            data = item
            if ident:
                avito_id = ident
            break
        if data:
            break
    if not avito_id:
        return None

    title = data.get("title")
    if isinstance(title, dict):
        title = title.get("text") or title.get("value")
    if not title:
        og = tree.css_first("meta[property='og:title']")
        if og and og.attributes.get("content"):
            title = og.attributes.get("content")
        else:
            title_node = tree.css_first("[data-marker='item-view/title-info'], h1")
            title = title_node.text(strip=True) if title_node else None

    price = _coerce_price(data) if data else None
    if price is None:
        price_node = tree.css_first("[itemprop='price'], [data-marker='item-view/item-price']")
        if price_node is not None:
            raw = price_node.attributes.get("content") or price_node.text()
            digits = re.sub(r"[^\d]", "", raw or "")
            price = int(digits) if digits else None

    return Card(
        avito_id=str(avito_id),
        url=url,
        title=title,
        price=price,
    )


def main() -> None:
    raw_dir = Path("data/raw")
    if len(sys.argv) > 1:
        raw_dir = Path(sys.argv[1])

    files = sorted(raw_dir.glob("*_card.html.gz"))
    if not files:
        print(f"No *_card.html.gz found in {raw_dir}")
        return

    print("=" * 135)
    header = (
        f"{'avito_id':<12} | {'price_old':<10} | {'price_new':<10} | "
        f"{'old_price':<10} | {'disc_%':<7} | {'Diff':<8} | {'Title':<65}"
    )
    print(header)
    print("-" * 135)

    total_cards = 0
    diff_count = 0
    new_found_price = 0
    old_missing_price = 0

    for fp in files:
        total_cards += 1
        aid = fp.name.split("_")[0]
        url = f"https://www.avito.ru/item/{aid}"

        with gzip.open(fp, "rt", encoding="utf-8", errors="replace") as f:
            html = f.read()

        c_old = parse_card_baseline(html, url=url)
        c_new = parse_card_new(html, url=url)

        p_old = c_old.price if c_old else None
        p_new = c_new.price if c_new else None
        p_old_price = c_new.old_price if c_new else None
        p_disc = f"{c_new.discount_pct:.0f}%" if c_new and c_new.discount_pct else "-"
        title = (c_new.title if c_new and c_new.title else (c_old.title if c_old else ""))[:63]

        diff = "NO"
        if p_old != p_new:
            diff = "MISMATCH"
            diff_count += 1
            if p_old is None and p_new is not None:
                new_found_price += 1
            elif p_old is not None and p_new is None:
                old_missing_price += 1

        print(
            f"{aid:<12} | {str(p_old):<10} | {str(p_new):<10} | "
            f"{str(p_old_price):<10} | {p_disc:<7} | {diff:<8} | {title:<65}"
        )

    print("=" * 135)
    print(f"Total cards replayed: {total_cards}")
    print(f"Mismatches: {diff_count}")
    print(f"Price found in new but None in old: {new_found_price}")
    print(f"Price found in old but None in new: {old_missing_price}")


if __name__ == "__main__":
    main()
