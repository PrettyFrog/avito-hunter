"""T-404/T-405: Leaf helpers for start_add_verification phase 3, 4, 5."""

from typing import Any

from loguru import logger

from hunter.core.db import Database
from hunter.core.models import User, now_utc
from hunter.core.product import ProductCoverage, ProductMatch, ProductProfile


async def check_admin_add_budget(db: Database) -> tuple[str, str, int]:
    factual_24h = 0
    try:
        conn = await db.connect()
        cur = await conn.execute(
            """
            SELECT COUNT(*)
            FROM request_log
            WHERE datetime(ts) >= datetime('now', '-24 hours')
              AND req_type != 'photo'
            """
        )
        row = await cur.fetchone()
        if row:
            factual_24h = row[0] or 0
    except Exception as exc:
        logger.warning("Failed to count 24h requests for budget check: {}", exc)

    daily_cap = await db.get_daily_cap()
    warn_threshold = int(daily_cap * 0.90)
    expected_new_impact = 44.1
    forecast_after_add = factual_24h + expected_new_impact
    free_headroom = warn_threshold - forecast_after_add
    free_budget_right_now = warn_threshold - factual_24h

    budget_warning = ""
    if forecast_after_add >= warn_threshold:
        budget_warning = (
            f"⚠️ **Внимание:** После добавления фактический расход за 24ч может превысить "
            f"порог {warn_threshold} запросов ({forecast_after_add:.0f} >= {warn_threshold}, лимит {daily_cap})!"
        )

    budget_line = (
        f"Бюджет: после добавления прогнозируется ~{forecast_after_add:.0f} из {warn_threshold} запросов/сут "
        f"(свободно до порога: ~{max(0, int(free_headroom))} запросов)"
    )
    return budget_line, budget_warning, free_budget_right_now


async def build_admin_temp_profile(
    name: str,
    price: int,
    clean_url: str,
    product_id: str,
    hits_data: list[dict[str, Any]],
    current_user: User | None = None,
) -> tuple[ProductProfile, str, list[tuple[str, float]]]:
    policy = "strict" if any(c.isdigit() for c in name) else "loose"
    user_min_interval = getattr(current_user, "min_interval_minutes", 15) if current_user else 15
    poll_mins = user_min_interval if user_min_interval and user_min_interval <= 10 else None

    temp_profile = ProductProfile(
        id=product_id,
        name=name,
        search_url=clean_url,
        max_price=price,
        headroom_pct=0.20,
        priority="normal",
        mode="live",
        poll_minutes=poll_mins,
        requirements=f"Оригинальный {name} в исправном состоянии.",
        match=ProductMatch(
            policy=policy,
            aliases=[],
            must_have=[],
            reject=[],
            loose_words=[],
            accessory_words=[],
        ),
        coverage=ProductCoverage(
            backfill=False,
            deep_sweep="off",
            max_pages=20,
        ),
        created_at=now_utc().isoformat(),
        last_confirmed_at=now_utc().isoformat(),
    )
    return temp_profile, policy, []


def evaluate_hits_match(
    hits_data: list[dict[str, Any]],
    temp_profile: ProductProfile,
    headroom_max: int,
) -> tuple[int, int, list[dict[str, Any]], list[dict[str, Any]]]:
    accepted_examples: list[dict[str, Any]] = []
    rejected_examples: list[dict[str, Any]] = []
    price_ok_count = 0
    passing_count = 0

    for h in hits_data:
        h_price = h.get("price") or 0
        h_title = h.get("title", "")
        if h_price <= headroom_max:
            price_ok_count += 1
            passing_count += 1
            if len(accepted_examples) < 3:
                accepted_examples.append({
                    "title": h_title,
                    "price": h_price,
                    "url": h.get("url", ""),
                    "priority": "normal",
                })
        else:
            if len(rejected_examples) < 3:
                reason = f"цена выше потолка ({h_price} > {headroom_max})"
                rejected_examples.append({
                    "title": h_title,
                    "price": h_price,
                    "url": h.get("url", ""),
                    "reason": reason,
                })
    return price_ok_count, passing_count, accepted_examples, rejected_examples


async def finalize_admin_add_verification(
    db: Database,
    chat_id: int,
    status_msg: Any,
    payload: dict[str, Any],
    render_summary_fn: Any,
) -> None:
    await db.save_pending_product(chat_id, "await_add_confirm", payload, ttl_hours=24)
    await render_summary_fn(status_msg, payload)
