"""T-400/T-401: Leaf helpers for start_add_verification_user phase 1 & 2."""

import asyncio
import hashlib
import html
import json
import re
import time
from datetime import datetime
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from loguru import logger

from hunter.core.db import Database
from hunter.core.models import User, now_utc
from hunter.core.product import get_product_store
from hunter.normalize.avito_url import normalize_avito_url
from hunter.normalize.text import normalize_text
from hunter.notify.messages_ru import ADD_CANCEL_BTN
from hunter.notify.telegram_router_parts.originalproductstore_01 import _safe_edit_text


async def validate_and_save_add_url(
    url: str,
    price: int,
    payload: dict[str, Any],
    db: Database,
    chat_id: int,
    user_id: int,
    status_msg: Message,
) -> tuple[str | None, bool]:
    clean_url, err = normalize_avito_url(url, price, headroom_pct=0.20)
    if err:
        payload["last_error"] = err
        await db.save_pending_product(
            chat_id, "await_add_url_edit", payload, ttl_hours=24, owner_user_id=user_id
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✏️ Изменить ссылку", callback_data="add_verify_edit_url"
                    )
                ],
                [InlineKeyboardButton(text=ADD_CANCEL_BTN, callback_data="add_cancel")],
            ]
        )
        await status_msg.edit_text(
            f"❌ {err}\n\nПожалуйста, отправьте корректную ссылку на поиск:", reply_markup=kb
        )
        return None, False
    return clean_url, True


def generate_user_product_id(user_id: int, name: str, store: Any = None) -> str:
    clean_id_base = re.sub(r"[^a-zA-Z0-9_]+", "_", normalize_text(name).lower()).strip("_")
    if not clean_id_base or clean_id_base[0].isdigit():
        clean_id_base = f"p_{clean_id_base}"

    if store is None:
        store = get_product_store()
    existing_profiles, _ = store.load_all()
    product_id = f"u{user_id}_{clean_id_base}"
    idx = 2
    while product_id in existing_profiles:
        product_id = f"u{user_id}_{clean_id_base}_{idx}"
        idx += 1
    return product_id


async def check_test_fetch_cache(db: Database, clean_url: str) -> tuple[list[dict[str, Any]], bool]:
    url_hash = hashlib.sha256(clean_url.encode()).hexdigest()[:16]
    cache_key = f"test_fetch_cache:{url_hash}"
    cached_raw = await db.get_runtime(cache_key)
    hits_data: list[dict[str, Any]] = []
    is_cached = False

    if cached_raw:
        try:
            cached_data = json.loads(cached_raw)
            cached_ts = cached_data.get("ts")
            if cached_ts:
                age_sec = (now_utc() - datetime.fromisoformat(cached_ts)).total_seconds()
                if age_sec < 600:
                    hits_data = cached_data.get("result", {}).get("hits", [])
                    is_cached = True
                    logger.info(
                        "Using cached test_fetch result for {} (age {:.1f}s)", clean_url, age_sec
                    )
        except Exception as parse_err:
            logger.debug("Failed to read test_fetch cache: {}", parse_err)
    return hits_data, is_cached


async def _poll_test_fetch_job(
    db: Database, result_key: str, job_id: int | None, timeout_sec: float = 180.0
) -> dict[str, Any] | None:
    poll_start = time.time()
    while time.time() - poll_start < timeout_sec:
        res_raw = await db.get_runtime(result_key)
        if res_raw:
            try:
                return json.loads(res_raw)
            except Exception:
                pass
            break
        if job_id:
            job = await db.get_job(job_id)
            if job and job.get("status") in ("poison", "failed"):
                err_msg = job.get("last_error") or "Ошибка выполнения"
                return {"status": "error", "error": err_msg, "hits": []}
        await asyncio.sleep(2.0)
    return None


def _format_test_fetch_error(raw_err: str) -> str:
    err_lower = raw_err.lower()
    if "circuit until" in err_lower or "circuit" in err_lower or "429" in err_lower:
        return (
            "Авито временно ограничивает частые запросы (HTTP 429). Подождите 1–2 "
            "минуты и нажмите «Попробовать снова»."
        )
    if "капча" in err_lower or "captcha" in err_lower:
        return "Авито запросил проверку капчи. Владелец бота уже оповещён."
    return raw_err


async def execute_test_fetch_job(
    db: Database,
    clean_url: str,
    product_id: str,
    payload: dict[str, Any],
    chat_id: int,
    user_id: int,
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

    result_dict = await _poll_test_fetch_job(db, result_key, job_id)
    if result_dict is None:
        user_err = "Не удалось выполнить тестовый поиск за 3 минуты (тайм-аут сервиса)."
    elif result_dict.get("status") == "error":
        user_err = _format_test_fetch_error(result_dict.get("error", "неизвестная ошибка"))
    else:
        return result_dict.get("hits", []), True

    payload["last_error"] = user_err
    await db.save_pending_product(
        chat_id, "await_add_verify_failed", payload, ttl_hours=24, owner_user_id=user_id
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Попробовать снова", callback_data="add_verify_retry")],
            [InlineKeyboardButton(text="✏️ Изменить ссылку", callback_data="add_verify_edit_url")],
            [InlineKeyboardButton(text=ADD_CANCEL_BTN, callback_data="add_cancel")],
        ]
    )
    await _safe_edit_text(
        status_msg,
        f"⚠️ <b>Не удалось проверить ссылку</b>\n\n"
        f"{html.escape(user_err)}\n\n"
        "Все введённые данные (название, цена, фото) сохранены. Выберите действие:",
        reply_markup=kb,
        parse_mode="HTML",
    )
    return None, False
