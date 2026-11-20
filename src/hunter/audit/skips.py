from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from hunter.core.db import Database
from hunter.core.models import now_utc
from hunter.core.product import get_product_store
from hunter.audit.skips_parts.audit_skips_01 import (
    build_feed_ceilings,
    build_stratified_sample,
    collect_skip_breakdown_and_samples,
    dispatch_audit_telegram_samples,
    query_skipped_listings,
)


def classify_skip_reason(
    title: str,
    description: str,
    verdict: str | None,
    skip_reason: str | None,
    reasons_list: list[str],
    event_reason: str | None,
    target_id: str,
) -> str:
    """Classifies skip reason into standard categories (C1):
    gem_prefilter, title_other_brand, title_other_model, model_unconfirmed, vision_skip, red_flag, other.
    """
    all_reasons = " ".join([skip_reason or "", event_reason or ""] + reasons_list).lower()
    title_lower = (title or "").lower()

    if "red_flag" in all_reasons:
        return "red_flag"

    if "form_factor" in all_reasons:
        return "title_other_model"

    if any(k in all_reasons for k in ("photo_other_model", "ocr_other_model", "vision_unavailable", "reference_mismatch", "vision")):
        return "vision_skip"

    if any(k in all_reasons for k in ("gem_prefilter", "gem_junk", "accessory")):
        return "gem_prefilter"

    # Brand checks
    target_brand = "sony" if "sony" in target_id else ("bose" if "bose" in target_id else "")
    alien_brands = ["sennheiser", "apple", "airpods", "marshall", "jbl", "beats", "bowers", "b&w", "audio-technica", "samsung", "huawei", "xiaomi"]
    if target_brand:
        alien_brands = [b for b in alien_brands if b != target_brand]
        if any(b in title_lower for b in alien_brands) or "other_brand" in all_reasons:
            return "title_other_brand"

    # Other model checks in title
    if "sony" in target_id:
        if re.search(r"\b(xm[1-5]|1000xm[1-5]|ch[0-9]{3}|xb[0-9]{3}|mdr|linkbuds|inzone|wf-)\b", title_lower) or "other_model" in all_reasons:
            return "title_other_model"
    elif "bose" in target_id:
        if re.search(r"\b(qc[0-9]{2}|quietcomfort\s*(35|45|15)|nc\s*700|ultra\s*(gen\s*1|1st|earbuds))\b", title_lower) or "other_model" in all_reasons:
            return "title_other_model"

    if any(k in all_reasons for k in ("title_other_model", "model_mismatch")):
        return "title_other_model"

    if "model_unconfirmed" in all_reasons or "loose_policy_skipped" in all_reasons:
        return "model_unconfirmed"

    if "prefilter" in all_reasons or "reject" in all_reasons:
        return "gem_prefilter"

    return "other"


def check_desc_contains_target(desc: str, target_id: str) -> str:
    """Checks whether description contains brand and/or target model."""
    if not desc:
        return "нет описания"
    desc_lower = desc.lower()
    is_sony = "sony" in target_id
    is_bose = "bose" in target_id

    has_brand = ("sony" in desc_lower) if is_sony else (("bose" in desc_lower) if is_bose else False)
    has_model = False
    if is_sony:
        has_model = bool(re.search(r"\b(xm6|1000xm6|wh-1000xm6|wh 1000 xm6)\b", desc_lower))
    elif is_bose:
        has_model = bool(re.search(r"\b(ultra\s*(?:gen\s*2|2nd|2)|ultra 2)\b", desc_lower))

    if has_brand and has_model:
        return "бренд + модель"
    elif has_model:
        return "модель"
    elif has_brand:
        return "бренд"
    return "нет"


async def audit_skips(
    db: Database,
    days: int = 7,
    n: int = 40,
    send_telegram: bool = False,
    notifier: Any = None,
) -> dict[str, Any]:
    """Audits skipped lots in gem feeds over the past N days (C1).
    Returns skip breakdown by reasons and a stratified sample of skipped lots with price <= ceiling.
    """
    ps = get_product_store()
    conn = await db.connect()

    cutoff_ts = (now_utc() - timedelta(days=days)).isoformat() if days > 0 else "1970-01-01"

    # Ceilings by feed
    ceilings = build_feed_ceilings(ps)

    # Query listings
    rows = await query_skipped_listings(conn, cutoff_ts)

    breakdown_by_feed, eligible_samples_by_reason = collect_skip_breakdown_and_samples(
        rows, ceilings
    )

    stratified_sample = build_stratified_sample(eligible_samples_by_reason, n)

    sent_count = 0
    if send_telegram and notifier:
        sent_count = await dispatch_audit_telegram_samples(stratified_sample, notifier)

    return {
        "days": days,
        "breakdown": breakdown_by_feed,
        "sample": stratified_sample,
        "sample_count": len(stratified_sample),
        "sent_count": sent_count,
        "ceilings": ceilings,
    }
