"""T-398: Leaf extract for product card formatting and keyboards."""

from datetime import UTC, datetime
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from loguru import logger

from hunter.core.db import Database
from hunter.core.models import now_utc
from hunter.core.product import ProductProfile
from hunter.notify.messages_ru import (
    MY_BTN_DELETE,
    MY_BTN_EDIT_PRICE,
    MY_BTN_FINDINGS,
    MY_BTN_PAUSE,
    MY_BTN_RESUME,
    MY_CARD_TEMPLATE,
    MY_NO_FINDINGS_YET,
    MY_STATUS_PAUSED,
    MY_STATUS_SEARCHING,
)


async def _format_product_card(p: ProductProfile, db: Database) -> str:
    status = MY_STATUS_SEARCHING if p.mode != "off" else MY_STATUS_PAUSED
    status_icon = "🟢" if p.mode != "off" else "⏸"
    price_str = f"{p.max_price:,}".replace(",", " ")
    last_found = await _get_last_found_time_ru(db, p.id)
    return MY_CARD_TEMPLATE.format(
        name=p.name or p.id,
        price=price_str,
        status=f"{status_icon} {status}",
        last_found=last_found,
    )


def _product_card_keyboard(p: ProductProfile) -> InlineKeyboardMarkup:
    pause_resume_btn = (
        InlineKeyboardButton(text=MY_BTN_PAUSE, callback_data=f"my_pause:{p.id}")
        if p.mode != "off"
        else InlineKeyboardButton(text=MY_BTN_RESUME, callback_data=f"my_resume:{p.id}")
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [pause_resume_btn],
            [
                InlineKeyboardButton(text=MY_BTN_EDIT_PRICE, callback_data=f"my_price:{p.id}"),
                InlineKeyboardButton(text=MY_BTN_FINDINGS, callback_data=f"my_findings:{p.id}"),
            ],
            [
                InlineKeyboardButton(text=MY_BTN_DELETE, callback_data=f"my_del_ask:{p.id}"),
            ],
        ]
    )


async def _get_last_found_time_ru(db: Database, product_id: str) -> str:
    try:
        conn = await db.connect()
        cur = await conn.execute(
            """
            SELECT l.first_seen, n.sent_at
            FROM listings l
            LEFT JOIN notifications n ON l.avito_id = n.avito_id
            WHERE l.watchlist_id = ?
            ORDER BY l.rowid DESC LIMIT 1
            """,
            (product_id,),
        )
        row = await cur.fetchone()
        if not row:
            return MY_NO_FINDINGS_YET
        ts_str = row[1] or row[0]
        if not ts_str:
            return MY_NO_FINDINGS_YET
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        diff_sec = max(0, int((now_utc() - dt).total_seconds()))
        if diff_sec < 60:
            return "только что"
        if diff_sec < 3600:
            mins = diff_sec // 60
            return f"{mins} мин. назад"
        if diff_sec < 86400:
            hours = diff_sec // 3600
            return f"{hours} ч. назад"
        days = diff_sec // 86400
        return f"{days} дн. назад"
    except Exception as exc:
        logger.debug("Failed to calculate last found time for {}: {}", product_id, exc)
        return MY_NO_FINDINGS_YET


async def _get_findings_for_product(
    db: Database, product_id: str, limit: int = 5
) -> list[dict[str, Any]]:
    try:
        conn = await db.connect()
        cur = await conn.execute(
            """
            SELECT l.title, l.price, l.url, COALESCE(n.sent_at, l.first_seen) as ts
            FROM listings l
            LEFT JOIN notifications n ON l.avito_id = n.avito_id
            WHERE l.watchlist_id = ?
            ORDER BY l.rowid DESC LIMIT ?
            """,
            (product_id, limit),
        )
        rows = await cur.fetchall()
        results = []
        for r in rows:
            ts_str = r[3]
            time_ago = "только что"
            if ts_str:
                try:
                    dt = datetime.fromisoformat(ts_str)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=UTC)
                    diff_sec = max(0, int((now_utc() - dt).total_seconds()))
                    if diff_sec < 60:
                        time_ago = "только что"
                    elif diff_sec < 3600:
                        time_ago = f"{diff_sec // 60} мин. назад"
                    elif diff_sec < 86400:
                        time_ago = f"{diff_sec // 3600} ч. назад"
                    else:
                        time_ago = f"{diff_sec // 86400} дн. назад"
                except Exception:
                    pass
            results.append(
                {
                    "title": r[0] or "Без названия",
                    "price": r[1],
                    "url": r[2] or "https://www.avito.ru",
                    "time_ago": time_ago,
                }
            )
        return results
    except Exception as exc:
        logger.debug("Failed to get findings for product {}: {}", product_id, exc)
        return []


async def _get_user_recent_findings(
    db: Database, user_id: int, product_ids: list[str], limit: int = 10
) -> list[dict[str, Any]]:
    if not product_ids:
        return []
    try:
        conn = await db.connect()
        placeholders = ",".join("?" for _ in product_ids)
        params: list[Any] = list(product_ids)
        params.append(limit)
        cur = await conn.execute(
            f"""
            SELECT l.title, l.price, l.url, COALESCE(n.sent_at, l.first_seen) as ts
            FROM listings l
            LEFT JOIN notifications n ON l.avito_id = n.avito_id
            WHERE l.watchlist_id IN ({placeholders})
            ORDER BY l.rowid DESC LIMIT ?
            """,
            params,
        )
        rows = await cur.fetchall()
        results = []
        for r in rows:
            ts_str = r[3]
            time_ago = "только что"
            if ts_str:
                try:
                    dt = datetime.fromisoformat(ts_str)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=UTC)
                    diff_sec = max(0, int((now_utc() - dt).total_seconds()))
                    if diff_sec < 60:
                        time_ago = "только что"
                    elif diff_sec < 3600:
                        time_ago = f"{diff_sec // 60} мин. назад"
                    elif diff_sec < 86400:
                        time_ago = f"{diff_sec // 3600} ч. назад"
                    else:
                        time_ago = f"{diff_sec // 86400} дн. назад"
                except Exception:
                    pass
            results.append(
                {
                    "title": r[0] or "Без названия",
                    "price": r[1],
                    "url": r[2] or "https://www.avito.ru",
                    "time_ago": time_ago,
                }
            )
        return results
    except Exception as exc:
        logger.debug("Failed to get recent user findings: {}", exc)
        return []
