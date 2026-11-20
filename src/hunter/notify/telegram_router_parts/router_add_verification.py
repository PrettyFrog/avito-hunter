"""Out-of-line Telegram add verification and summary rendering helpers."""

from __future__ import annotations

from hunter.notify.telegram_router_parts import router_globals as _router_globals


async def _prepare_user_add_verification(
    message: _router_globals.Message,
    payload: dict[str, _router_globals.Any],
    db: _router_globals.Database,
    current_user: _router_globals.User,
    status_msg: _router_globals.Message,
) -> tuple[str, str, _router_globals.Any, list[_router_globals.Any]] | None:
    clean_url, ok = await _router_globals.validate_and_save_add_url(
        payload["search_url"],
        payload["price"],
        payload,
        db,
        message.chat.id,
        current_user.id,
        status_msg,
    )
    if not ok or clean_url is None:
        return None

    product_id = _router_globals.generate_user_product_id(
        current_user.id, payload["name"], store=_router_globals.get_product_store()
    )
    hits_data, is_cached = await _router_globals.check_test_fetch_cache(db, clean_url)
    if not is_cached:
        hits_data, ok = await _router_globals.execute_test_fetch_job(
            db, clean_url, product_id, payload, message.chat.id, current_user.id, status_msg
        )
        if not ok or hits_data is None:
            return None

    min_interval = getattr(current_user, "min_interval_minutes", 15) or 15
    temp_profile = await _router_globals.build_temp_product_profile(
        payload["name"],
        payload["price"],
        clean_url,
        product_id,
        payload.get("policy", "strict"),
        payload.get("requirements", ""),
        current_user.id,
        min_interval,
        hits_data,
    )
    return clean_url, product_id, temp_profile, hits_data


async def _prepare_admin_add_payload(
    db: _router_globals.Database,
    name: str,
    price: int,
    clean_url: str,
    product_id: str,
    hits_data: list[_router_globals.Any],
    attached_photos: list[str],
) -> dict[str, _router_globals.Any]:
    (
        budget_line,
        budget_warning,
        free_budget_right_now,
    ) = await _router_globals.check_admin_add_budget(db)
    temp_profile, policy, close_tokens = await _router_globals.build_admin_temp_profile(
        name, price, clean_url, product_id, hits_data
    )
    headroom_max = int(price * 1.20)
    price_ok, passing, accepted, rejected = _router_globals.evaluate_hits_match(
        hits_data, temp_profile, headroom_max
    )
    return {
        "product": temp_profile.model_dump(),
        "name": name,
        "price": price,
        "clean_url": clean_url,
        "policy": policy,
        "hits_count": len(hits_data),
        "price_ok_count": price_ok,
        "passing_count": passing,
        "accepted_examples": accepted,
        "rejected_examples": rejected,
        "close_tokens": close_tokens,
        "free_budget_right_now": free_budget_right_now,
        "budget_line": budget_line,
        "budget_warning": budget_warning,
        "photos": attached_photos,
    }


async def _start_add_verification_user(
    message: _router_globals.Message,
    payload: dict[str, _router_globals.Any],
    db: _router_globals.Database,
    current_user: _router_globals.User,
) -> None:
    status_msg = await message.reply(_router_globals.ADD_PREVIEW_CHECKING)
    name = payload["name"]
    price = payload["price"]
    policy = payload.get("policy", "strict")
    requirements = payload.get("requirements", "")

    # 1. URL Normalization
    prepared = await _prepare_user_add_verification(message, payload, db, current_user, status_msg)
    if prepared is None:
        return
    clean_url, product_id, temp_profile, hits_data = prepared
    text, kb = _router_globals.render_add_confirm_card(
        name, price, policy, requirements, payload.get("photos", []), len(hits_data)
    )
    await _router_globals.finalize_add_verification_user(
        message,
        status_msg,
        db,
        payload,
        temp_profile,
        clean_url,
        product_id,
        current_user.id,
        text,
        kb,
    )


async def _start_add_verification(
    message: _router_globals.Message,
    name: str,
    price: int,
    url: str,
    db: _router_globals.Database,
    attached_photos: list[str],
) -> None:
    status_msg = await message.reply("⏳ Проверяю ссылку и тестовую выдачу Авито через сервис...")

    # 1. URL Normalization
    clean_url, ok = await _router_globals.validate_admin_add_url(
        url, price, message.chat.id, db, status_msg
    )
    if not ok or clean_url is None:
        return

    # Clean ID base & product_id
    product_id = _router_globals.generate_admin_product_id(
        name, store=_router_globals.ProductStore()
    )

    # 2. Test Fetch via jobs queue with 10-minute cache
    hits_data, ok = await _router_globals.fetch_test_hits_admin(
        db, clean_url, product_id, message.chat.id, status_msg
    )
    if not ok or hits_data is None:
        return

    payload = await _prepare_admin_add_payload(
        db, name, price, clean_url, product_id, hits_data, attached_photos
    )

    await _router_globals.finalize_admin_add_verification(
        db, message.chat.id, status_msg, payload, _router_globals._render_summary_card
    )


async def _render_summary_card(
    target_msg: _router_globals.Any, payload: dict[str, _router_globals.Any]
) -> None:
    p = payload["product"]
    backfill_on = p.get("coverage", {}).get("backfill", False)
    free_budget = payload.get("free_budget_right_now", 0)
    kb = _router_globals.build_summary_card_keyboard(free_budget, backfill_on)
    text = _router_globals._format_summary_card_text(payload)
    await _router_globals.send_summary_card(target_msg, text, kb)
