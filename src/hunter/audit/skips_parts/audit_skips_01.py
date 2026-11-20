from __future__ import annotations

import json
from typing import Any


def build_feed_ceilings(ps: Any) -> dict[str, int]:
    ceilings: dict[str, int] = {}
    for prof in ps.profiles.values():
        if prof.match.policy == "loose" or "gem" in prof.id:
            headroom_max = int(prof.max_price * (1.0 + prof.headroom_pct))
            ceilings[prof.id] = headroom_max

    if not ceilings:
        ceilings = {
            "gem_headphones_sony": 19200,
            "gem_headphones_bose": 22800,
        }
    return ceilings


async def query_skipped_listings(conn: Any, cutoff_ts: str) -> list[Any]:
    query = """
    SELECT l.avito_id, l.title, l.price, l.url, l.watchlist_id, l.description, l.first_seen,
           e.verdict, e.skip_reason, e.reasons
    FROM listings l
    LEFT JOIN evaluations e ON l.avito_id = e.avito_id
    WHERE l.watchlist_id LIKE 'gem_%'
      AND (l.first_seen >= ? OR l.last_seen >= ?)
    ORDER BY l.first_seen DESC
    """
    cur = await conn.execute(query, (cutoff_ts, cutoff_ts))
    return await cur.fetchall()


def collect_skip_breakdown_and_samples(
    rows: list[Any],
    ceilings: dict[str, int],
) -> tuple[dict[str, dict[str, int]], dict[str, list[dict[str, Any]]]]:
    from hunter.audit.skips import check_desc_contains_target, classify_skip_reason

    breakdown_by_feed: dict[str, dict[str, int]] = {}
    eligible_samples_by_reason: dict[str, list[dict[str, Any]]] = {}

    standard_reasons = [
        "gem_prefilter",
        "title_other_brand",
        "title_other_model",
        "model_unconfirmed",
        "vision_skip",
        "red_flag",
        "other",
    ]

    for r in rows:
        aid, title, price, url, wid, desc, first_seen, verdict, skip_reason, reasons_raw = r
        reasons_list = json.loads(reasons_raw) if reasons_raw else []

        # Only audit skipped lots (verdict != buy_now, ask_questions, watch)
        is_skipped = (verdict == "skip") or (verdict is None and skip_reason is not None) or (verdict is None)
        if not is_skipped:
            continue

        c_reason = classify_skip_reason(
            title=title or "",
            description=desc or "",
            verdict=verdict,
            skip_reason=skip_reason,
            reasons_list=reasons_list,
            event_reason=None,
            target_id=wid,
        )

        feed_counts = breakdown_by_feed.setdefault(wid, {k: 0 for k in standard_reasons})
        feed_counts[c_reason] = feed_counts.get(c_reason, 0) + 1

        # Strict owner rule: price criterion is price <= ceiling. NEVER include lots above ceiling!
        max_allowed = ceilings.get(wid, 999999)
        if price is not None and price <= max_allowed:
            desc_tag = check_desc_contains_target(desc or "", wid)
            sample_entry = {
                "avito_id": aid,
                "title": title or aid,
                "price": price,
                "url": url,
                "watchlist_id": wid,
                "reason": c_reason,
                "raw_reason": skip_reason or (reasons_list[0] if reasons_list else ""),
                "desc_target": desc_tag,
            }
            eligible_samples_by_reason.setdefault(c_reason, []).append(sample_entry)

    return breakdown_by_feed, eligible_samples_by_reason



def build_stratified_sample(
    eligible_samples_by_reason: dict[str, list[dict[str, Any]]],
    n: int,
) -> list[dict[str, Any]]:
    stratified_sample: list[dict[str, Any]] = []
    if not eligible_samples_by_reason:
        return stratified_sample

    reasons_with_items = [r for r, items in eligible_samples_by_reason.items() if items]
    per_reason_target = max(1, n // len(reasons_with_items)) if reasons_with_items else n
    for r_name in reasons_with_items:
        stratified_sample.extend(eligible_samples_by_reason[r_name][:per_reason_target])

    # Fill remainder up to n
    if len(stratified_sample) < n:
        for r_name in reasons_with_items:
            for item in eligible_samples_by_reason[r_name][per_reason_target:]:
                if len(stratified_sample) >= n:
                    break
                stratified_sample.append(item)
            if len(stratified_sample) >= n:
                break
    return stratified_sample


async def dispatch_audit_telegram_samples(
    stratified_sample: list[dict[str, Any]],
    notifier: Any,
) -> int:
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    from loguru import logger

    sent_count = 0
    bot = await notifier._client() if hasattr(notifier, "_client") else getattr(notifier, "bot", None)
    admin_id = getattr(notifier, "chat_id", None) or getattr(notifier, "admin_chat_id", None)

    if not (bot and admin_id):
        return 0

    for item in stratified_sample:
        try:
            aid = item["avito_id"]
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="✅ верно отсеяли", callback_data=f"audit:ok:{aid}"),
                        InlineKeyboardButton(text="⚠ зря отсеяли", callback_data=f"audit:lost:{aid}"),
                    ]
                ]
            )
            text = (
                f"🕵️ **Аудит отсева (Gem лента: `{item['watchlist_id']}`)**\n"
                f"• **Товар:** {item['title']}\n"
                f"• **Цена:** {item['price']:,} ₽\n"
                f"• **Причина отсева:** `{item['reason']}` ({item['raw_reason'] or 'правило'})\n"
                f"• **В описании:** {item['desc_target']}\n"
                f"• **Ссылка:** [Открыть лот]({item['url']})"
            )
            await bot.send_message(
                chat_id=admin_id,
                text=text,
                parse_mode="Markdown",
                reply_markup=kb,
                disable_web_page_preview=True,
            )
            sent_count += 1
        except Exception as e:
            logger.debug("Failed to send audit lot {}: {}", item.get("avito_id"), e)
    return sent_count
