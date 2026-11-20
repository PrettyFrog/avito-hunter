"""Second set of pure decision evaluators: judge, formatting, roles, budget."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

from tools.parity.common import cleanup_temp_db, copy_temp_db

from hunter.budget.allocator import (
    build_budget_allocation,
    calculate_arrival_rate_lambda,
    compute_target_interval,
    get_stages_comparison_plan,
)
from hunter.core.db import Database
from hunter.core.models import (
    Card,
    Extraction,
    NotificationEvent,
    Round1Decision,
    WatchItem,
)
from hunter.core.product import apply_product_pause, clear_product_pause
from hunter.llm.router import LLMRouter
from hunter.notify.telegram import format_event, format_user_alert
from hunter.notify.vk import VkNotifier
from hunter.pipeline.judge import (
    check_ocr_names_other_model,
    is_model_confirmed_by_title,
    run_judge,
)


def _build_judge_test_fixture() -> tuple[Card, Round1Decision, Extraction, WatchItem]:
    """Create test card, decision, extraction, and watch item for judge evaluation."""
    card = Card(
        avito_id="301",
        title="Sony WH-1000XM6 Black",
        price=26000,
        url="https://avito.ru/301",
        description="Отличное состояние, полный комплект.",
    )
    r1 = Round1Decision(pass_stub=True)
    ext = Extraction(model_id="Sony WH-1000XM6", confidence=0.95, red_flags=[])
    watch = WatchItem(
        id="sony_xm6",
        category="headphones",
        models=["Sony WH-1000XM6"],
        max_price=30000,
        search_urls=["https://avito.ru/moskva/audio_i_video"],
    )
    return card, r1, ext, watch


async def evaluate_final_verdict_judge() -> dict[str, Any]:
    """Evaluate pure final judge decisions across various signal combinations."""
    from tests.fixtures.arch_state import get_synthetic_parity_profiles

    profiles = get_synthetic_parity_profiles()
    p = profiles["gem_headphones_sony"]
    card, r1, ext, watch = _build_judge_test_fixture()

    async def mock_complete(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "verdict": "NOTIFY",
            "confidence": 0.95,
            "model_confirmed": True,
            "is_replica": False,
            "has_critical_defect": False,
            "reasons": ["Оригинальные наушники по хорошей цене"],
        }

    with patch.object(LLMRouter, "complete_json", side_effect=mock_complete):
        res_clean = await run_judge(
            card,
            r1,
            ext,
            watch=watch,
            product=p,
            vision_report={"match": True, "confidence": 0.9, "detected_model": "Sony WH-1000XM6"},
        )
        res_mismatch = await run_judge(
            card,
            r1,
            ext,
            watch=watch,
            product=p,
            vision_report={"match": False, "ocr_text": "Model: WH-1000XM5", "mismatch": True},
        )

    confirmed = is_model_confirmed_by_title(card, watch, ext)
    mismatch_found, ocr_mod = check_ocr_names_other_model(
        {"ocr_text": "Sony WH-1000XM5 Black"}, "sony_wh_1000xm6", "headphones"
    )

    return {
        "clean_verdict": res_clean.model_dump(),
        "mismatch_verdict": res_mismatch.model_dump(),
        "is_model_confirmed": confirmed,
        "ocr_mismatch_check": {"mismatch_found": mismatch_found, "model": ocr_mod},
    }


def evaluate_message_formatting() -> dict[str, Any]:
    """Evaluate pure notification formatting for Telegram and VK."""
    dt = datetime(2026, 10, 22, 12, 0, 0, tzinfo=UTC)
    ev_standard = NotificationEvent(
        avito_id="401",
        url="https://avito.ru/401",
        title="Sony WH-1000XM6",
        price=24000,
        max_price=30000,
        watchlist_id="sony_xm6",
        category="headphones",
        model_id="sony_wh_1000xm6",
        verdict="NOTIFY",
        reasons=["Хорошая цена", "Проверенный продавец"],
        specs={"battery_health_pct": 91, "battery_cycles": 85},
    )
    ev_price_drop = NotificationEvent(
        avito_id="402",
        url="https://avito.ru/402",
        title="Bose QC Ultra",
        price=22000,
        max_price=30000,
        watchlist_id="bose_ultra",
        category="headphones",
        model_id="bose_quietcomfort_ultra",
        verdict="NOTIFY",
        reasons=["Снижение цены"],
        price_reduced_from_above_max=(28000, 22000),
    )

    notifier = VkNotifier()

    return {
        "tg_standard": format_event(ev_standard),
        "user_alert_standard": format_user_alert(ev_standard),
        "vk_standard": notifier.format_message(ev_standard),
        "tg_price_drop": format_event(ev_price_drop),
        "vk_price_drop": notifier.format_message(ev_price_drop),
    }


def evaluate_handler_roles() -> dict[str, Any]:
    """Evaluate pure worker role handlers and pause permission hierarchies."""
    from tests.fixtures.arch_state import get_synthetic_parity_profiles

    profiles = get_synthetic_parity_profiles()
    p = profiles["gem_headphones_sony"]

    transitions: list[dict[str, Any]] = []

    # 1. Owner pauses
    apply_product_pause(p, "owner_paused", actor_role="owner")
    transitions.append({"step": "owner_pause", "paused_reason": p.paused_reason})

    # 2. User tries to clear owner pause (must fail)
    u_res, _ = clear_product_pause(p, actor_role="user")
    transitions.append({"step": "user_clear_owner", "ok": u_res, "paused_reason": p.paused_reason})

    # 3. Owner clears pause (must succeed)
    o_res, _ = clear_product_pause(p, actor_role="owner")
    transitions.append({"step": "owner_clear_owner", "ok": o_res, "paused_reason": p.paused_reason})

    # 4. User pauses
    apply_product_pause(p, "user_paused", actor_role="user")
    transitions.append({"step": "user_pause", "paused_reason": p.paused_reason})

    # 5. User clears user pause (must succeed)
    u2_res, _ = clear_product_pause(p, actor_role="user")
    transitions.append({"step": "user_clear_user", "ok": u2_res, "paused_reason": p.paused_reason})

    job_dispatch_registry = {
        "shadow_probe": "_handle_shadow_probe_job",
        "test_fetch": "_handle_test_fetch_job",
        "photo_check": "_handle_photo_check_job",
        "catchup_page": "_handle_catchup_page_job",
        "backfill": "_handle_backfill_job",
        "deep_sweep": "_handle_deep_sweep_job",
    }

    return {"transitions": transitions, "job_dispatch_registry": job_dispatch_registry}


async def evaluate_schedule_budget() -> dict[str, Any]:
    """Evaluate schedule calculation, target intervals, and budget allocation on DB copy."""
    tmp_db_path = copy_temp_db()
    db = Database(tmp_db_path)
    await db.connect()
    try:
        lamb_sony = await calculate_arrival_rate_lambda(db, "gem_headphones_sony")
        lamb_bose = await calculate_arrival_rate_lambda(db, "bose_quietcomfort_ultra_gen_2")

        intervals = {
            "high_sony": compute_target_interval("high", lamb_sony),
            "high_bose": compute_target_interval("high", lamb_bose),
            "medium_test": compute_target_interval("medium", 2.5),
            "low_test": compute_target_interval("low", 0.5),
        }

        from tests.fixtures.arch_state import get_synthetic_parity_profiles
        profiles_list = [p for p in get_synthetic_parity_profiles().values() if p.mode != "off"]
        stage_plans = get_stages_comparison_plan(profiles_list)
        alloc = await build_budget_allocation(db, profiles_list)
        items_summary = [
            {
                "product_id": it.product_id,
                "name": it.name,
                "priority": it.priority,
                "current_interval_min": it.current_interval_min,
                "proposed_interval_min": it.proposed_interval_min,
                "daily_polls": it.daily_polls,
            }
            for it in alloc.items
        ]

        return {
            "lambdas": {"sony": lamb_sony, "bose": lamb_bose},
            "intervals": intervals,
            "stage_plans": [
                {"stage": s.get("stage"), "total_polls": s.get("total_polls")}
                for s in stage_plans
            ],
            "budget_allocation": {
                "allocated_total": alloc.allocated_total,
                "total_budget": alloc.total_budget,
                "stretched": alloc.stretched,
                "items": items_summary,
            },
        }
    finally:
        await db.close()
        cleanup_temp_db(tmp_db_path)
