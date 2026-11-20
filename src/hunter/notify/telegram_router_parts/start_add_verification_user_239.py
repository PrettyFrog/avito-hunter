"""T-402/T-403: Leaf helpers for start_add_verification_user and start_add_verification."""

import asyncio
from datetime import datetime
import hashlib
import html
import json
import re
import time
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from loguru import logger

from hunter.core.db import Database
from hunter.core.models import now_utc
from hunter.core.product import ProductMatch, ProductProfile, ProductStore
from hunter.normalize.avito_url import normalize_avito_url
from hunter.normalize.text import normalize_text
from hunter.notify.messages_ru import (
    ADD_CANCEL_BTN,
    ADD_CONFIRM_BTN,
    ADD_CONFIRM_CARD_EMPTY,
    ADD_CONFIRM_CARD_FOUND,
    ADD_EDIT_PRICE_BTN,
)


async def build_temp_product_profile(
    name: str,
    price: int,
    clean_url: str,
    product_id: str,
    policy: str,
    requirements: str,
    user_id: int,
    user_min_interval: int | None = 15,
    hits_data: list[dict[str, Any]] | None = None,
) -> ProductProfile:
    spec: dict[str, Any] = {}
    min_int = user_min_interval or 15
    poll_mins = min_int if min_int <= 10 else None

    return ProductProfile(
        id=product_id,
        name=name,
        search_url=clean_url,
        max_price=price,
        headroom_pct=0.20,
        priority="normal",
        mode="live",
        poll_minutes=poll_mins,
        requirements=requirements or spec.get("requirements", ""),
        match=ProductMatch(
            policy=policy,
            aliases=spec.get("aliases", []),
            must_have=spec.get("must_have", []),
            reject=spec.get("reject", []),
            loose_words=spec.get("loose_words", []),
            accessory_words=spec.get("accessory_words", []),
        ),
        owner_user_id=user_id,
        created_at=now_utc().isoformat(),
        last_confirmed_at=now_utc().isoformat(),
    )


def render_add_confirm_card(
    name: str,
    price: int,
    policy: str,
    requirements: str,
    photos: list[str],
    hits_count: int,
) -> tuple[str, InlineKeyboardMarkup]:
    photos_count = len(photos)
    photos_text = f"прикреплено ({photos_count} шт.)" if photos_count > 0 else "не прикреплены"
    policy_text = (
        "Только точное совпадение"
        if policy == "strict"
        else "Похожие тоже подойдут (проверка по фото)"
    )
    reqs_val = (requirements or "").strip()
    requirements_line = f"• Пожелания: <b>{html.escape(reqs_val)}</b>\n" if reqs_val else ""
    price_str = f"{price:,}".replace(",", " ")
    name_escaped = html.escape(name)

    if hits_count > 0:
        text = ADD_CONFIRM_CARD_FOUND.format(
            total=hits_count,
            name=name_escaped,
            price=price_str,
            policy_text=policy_text,
            photos_text=photos_text,
            requirements_line=requirements_line,
        )
    else:
        text = ADD_CONFIRM_CARD_EMPTY.format(
            name=name_escaped,
            price=price_str,
            policy_text=policy_text,
            photos_text=photos_text,
            requirements_line=requirements_line,
        )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=ADD_CONFIRM_BTN, callback_data="add_confirm_launch")],
            [InlineKeyboardButton(text=ADD_EDIT_PRICE_BTN, callback_data="add_edit_price")],
            [InlineKeyboardButton(text=ADD_CANCEL_BTN, callback_data="add_cancel")],
        ]
    )
    return text, kb


async def finalize_add_verification_user(
    message: Message,
    status_msg: Message,
    db: Database,
    payload: dict[str, Any],
    temp_profile: ProductProfile,
    clean_url: str,
    product_id: str,
    user_id: int,
    text: str,
    kb: InlineKeyboardMarkup,
) -> None:
    payload["product"] = temp_profile.model_dump()
    payload["clean_url"] = clean_url
    payload["product_id"] = product_id
    await db.save_pending_product(
        message.chat.id, "await_add_confirm", payload, ttl_hours=24, owner_user_id=user_id
    )
    await status_msg.edit_text(text, reply_markup=kb, parse_mode="HTML")


async def validate_admin_add_url(
    url: str, price: int, chat_id: int, db: Database, status_msg: Message
) -> tuple[str | None, bool]:
    clean_url, err = normalize_avito_url(url, price, headroom_pct=0.20)
    if err:
        await status_msg.edit_text(f"❌ {err}")
        await db.delete_pending_product(chat_id)
        return None, False
    return clean_url, True


def generate_admin_product_id(name: str, store: Any = None) -> str:
    clean_id_base = re.sub(r"[^a-zA-Z0-9_]+", "_", normalize_text(name).lower()).strip("_")
    if not clean_id_base or clean_id_base[0].isdigit():
        clean_id_base = f"p_{clean_id_base}"

    if store is None:
        store = ProductStore()
    existing_profiles, _ = store.load_all()
    product_id = clean_id_base
    idx = 2
    while product_id in existing_profiles:
        product_id = f"{clean_id_base}_{idx}"
        idx += 1
    return product_id


async def _check_admin_test_fetch_cache(
    db: Database, clean_url: str
) -> tuple[list[dict[str, Any]] | None, bool]:
    url_hash = hashlib.sha256(clean_url.encode()).hexdigest()[:16]
    cache_key = f"test_fetch_cache:{url_hash}"
    cached_raw = await db.get_runtime(cache_key)
    if cached_raw:
        try:
            cached_data = json.loads(cached_raw)
            cached_ts = cached_data.get("ts")
            if cached_ts:
                age_sec = (now_utc() - datetime.fromisoformat(cached_ts)).total_seconds()
                if age_sec < 600:
                    hits_data = cached_data.get("result", {}).get("hits", [])
                    logger.info("Using cached test_fetch result for {} (age {:.1f}s)", clean_url, age_sec)
                    return hits_data, True
        except Exception as parse_err:
            logger.debug("Failed to read test_fetch cache: {}", parse_err)
    return None, False


async def _execute_admin_test_fetch(
    db: Database,
    clean_url: str,
    product_id: str,
    chat_id: int,
    status_msg: Message,
) -> tuple[list[dict[str, Any]] | None, bool]:
    req_id = hashlib.sha256(f"{clean_url}_{time.time()}".encode()).hexdigest()[:16]
    result_key = f"test_fetch_result:{req_id}"
    job_id = await db.enqueue_job(
        kind="test_fetch",
        product_id=product_id,
        dedupe_key=f"tf:{req_id}",
        payload={"url": clean_url, "req_id": req_id, "product_id": product_id},
        priority=20,
    )

    poll_start = time.time()
    result_dict = None
    while time.time() - poll_start < 180.0:
        res_raw = await db.get_runtime(result_key)
        if res_raw:
            try:
                result_dict = json.loads(res_raw)
            except Exception:
                pass
            break
        if job_id:
            job = await db.get_job(job_id)
            if job and job.get("status") in ("poison", "failed"):
                err_msg = job.get("last_error") or "Ошибка выполнения"
                result_dict = {"status": "error", "error": err_msg, "hits": []}
                break
        await asyncio.sleep(2.0)

    if result_dict is None:
        await status_msg.edit_text(
            "❌ Сервис hunter недоступен или не успел выполнить тестовый запрос за 3 минуты.\n"
            "Добавление товара отменено (профиль не создан). Проверьте статус сервиса."
        )
        await db.delete_pending_product(chat_id)
        return None, False

    if result_dict.get("status") == "error":
        err_msg = result_dict.get("error", "неизвестная ошибка")
        await status_msg.edit_text(
            f"❌ Ошибка при выполнении тестового запроса: {err_msg}\n"
            "Добавление товара отменено (профиль не создан)."
        )
        await db.delete_pending_product(chat_id)
        return None, False

    return result_dict.get("hits", []), True


async def fetch_test_hits_admin(
    db: Database,
    clean_url: str,
    product_id: str,
    chat_id: int,
    status_msg: Message,
) -> tuple[list[dict[str, Any]] | None, bool]:
    cached_hits, is_cached = await _check_admin_test_fetch_cache(db, clean_url)
    if is_cached:
        return cached_hits, True
    return await _execute_admin_test_fetch(db, clean_url, product_id, chat_id, status_msg)
