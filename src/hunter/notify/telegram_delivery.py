"""Typed Telegram delivery adapter with error classification and photo fallback (S6 / T-486)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import html
import logging
from pathlib import Path
import re
from typing import Any, Literal
from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from hunter.application.dispatcher import SendResult
from hunter.notify.telegram_alert_card import (
    build_alert_keyboard,
    format_alert_caption,
    format_relative_time,
    format_rich_html,
    format_seller_line,
)
from hunter.storage.deliveries import ClaimedDelivery

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TelegramDeliveryEnvelope:
    chat_id: int | str
    text: str
    photo_input: Any = None
    photo_bytes: bytes | None = None
    photos_bytes: list[bytes] | None = None
    rich_html: str | None = None
    reply_markup: Any = None
    parse_mode: str | None = None
    delivery_id: str = ""
    event_id: str = ""
    user_id: int = 0


@dataclass(frozen=True, slots=True)
class TelegramDeliveryResult:
    status: Literal["sent", "retry", "blocked", "permanent", "ambiguous"]
    message_id: str = ""
    retry_after: int = 30
    error_message: str = ""
    retry_scope: Literal["delivery", "chat", "global"] = "delivery"

    def to_send_result(self) -> SendResult:
        if self.status == "sent":
            return SendResult(status="sent", message_id=self.message_id)
        if self.status in ("blocked", "permanent"):
            return SendResult(status="dead_letter", error_message=self.error_message)
        if self.status == "ambiguous":
            return SendResult(
                status="ambiguous",
                retry_after_seconds=self.retry_after,
                error_message=self.error_message,
            )
        return SendResult(
            status="retry", retry_after_seconds=self.retry_after,
            error_message=self.error_message, retry_scope=self.retry_scope,
        )


_AVITO_ITEM_ID_RE = re.compile(r"(?:_i?|/)(\d{8,14})(?:\?|$|/)")


def to_short_avito_url(url: str, avito_id: str | int | None = None) -> str:
    """Transforms any Avito listing URL or ID into clean short format https://www.avito.ru/{id}."""
    if avito_id is not None:
        aid = str(avito_id).strip()
        if aid.isdigit() and len(aid) >= 8:
            return f"https://www.avito.ru/{aid}"
    if not url:
        return ""
    m = _AVITO_ITEM_ID_RE.search(url)
    if m:
        return f"https://www.avito.ru/{m.group(1)}"
    return url.split("?")[0] if "?" in url else url


def escape_telegram_html(text: str) -> str:
    """Safely escapes text for Telegram HTML parse mode."""
    return html.escape(text, quote=False)


def format_delivery_text(
    title: str,
    price: int,
    url: str,
    old_price: int | None = None,
    is_html: bool = False,
    avito_id: str | int | None = None,
) -> str:
    safe_title = escape_telegram_html(title) if is_html else title
    clean_url = to_short_avito_url(url, avito_id=avito_id)
    if old_price is not None and old_price != price:
        lines = [f"📉 {safe_title}", f"Цена: {old_price} ₽ ➔ {price} ₽", clean_url]
    else:
        lines = [f"🎧 {safe_title}", f"Цена: {price} ₽", clean_url]
    return "\n".join(lines)


def _classify_bad_request(exc: TelegramBadRequest) -> str:
    msg = str(exc).lower()
    if any(t in msg for t in ("chat not found", "deactivated", "blocked", "chat was deleted")):
        return "blocked"
    return "permanent"


def _classify_acceptance(message: Any) -> TelegramDeliveryResult:
    message_id = getattr(message, "message_id", None)
    if type(message_id) is not int or message_id <= 0:
        return TelegramDeliveryResult(
            status="ambiguous", error_message="Telegram response lacks a valid message ID"
        )
    return TelegramDeliveryResult(status="sent", message_id=str(message_id))


async def fetch_photo_bytes(
    url: str, timeout: float = 4.0, client: Any = None
) -> bytes | None:
    """Fetch photo bytes via httpx using residential IP, bypassing Avito CDN blocking."""
    try:
        import httpx

        h = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)", "Referer": "https://www.avito.ru/"}
        if client is not None:
            resp = await client.get(url, headers=h)
            if resp.status_code == 200 and resp.content:
                return resp.content
        else:
            async with httpx.AsyncClient(timeout=timeout, trust_env=False) as c:
                resp = await c.get(url, headers=h)
                if resp.status_code == 200 and resp.content:
                    return resp.content
    except Exception as exc:
        logger.warning("photo fetch failed for %s: %s", url, exc)
    return None


async def fetch_photos_bytes(
    urls: list[str], timeout: float = 4.0, client: Any = None
) -> list[bytes]:
    """Fetch multiple photo bytes in parallel via residential IP buffer."""
    if not urls:
        return []
    tasks = [fetch_photo_bytes(u, timeout=timeout, client=client) for u in urls[:3]]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return [r for r in results if isinstance(r, bytes) and r]


def _classify_send_error(exc: Exception, kind: str) -> TelegramDeliveryResult | None:
    if isinstance(exc, TelegramRetryAfter):
        return TelegramDeliveryResult(status="retry", retry_after=exc.retry_after, error_message=str(exc), retry_scope="chat")
    if isinstance(exc, TelegramForbiddenError):
        return TelegramDeliveryResult(status="blocked", error_message=str(exc))
    if isinstance(exc, TelegramBadRequest):
        if _classify_bad_request(exc) == "blocked":
            return TelegramDeliveryResult(status="blocked", error_message=str(exc))
        logger.warning("telegram %s send failed (%s), falling back", kind, exc)
        return None
    if isinstance(exc, (TelegramNetworkError, TimeoutError, asyncio.TimeoutError)):
        return TelegramDeliveryResult(status="ambiguous", retry_after=30, error_message=str(exc))
    logger.warning("telegram %s send failed unexpectedly (%s), falling back", kind, exc)
    return None


async def _send_rich_attempt(
    bot: Bot, env: TelegramDeliveryEnvelope
) -> TelegramDeliveryResult | None:
    buffers = env.photos_bytes or ([env.photo_bytes] if env.photo_bytes else [])
    if not env.rich_html or not buffers:
        return None
    try:
        from aiogram.methods import SendRichMessage
        from aiogram.types import (
            BufferedInputFile,
            InputMediaPhoto,
            InputRichMessage,
            InputRichMessageMedia,
        )

        media = [
            InputRichMessageMedia(
                id=f"p{i+1}",
                media=InputMediaPhoto(media=BufferedInputFile(b, filename=f"p{i+1}.jpg")),
            )
            for i, b in enumerate(buffers[:4])
        ]
        msg = await bot(
            SendRichMessage(
                chat_id=env.chat_id,
                rich_message=InputRichMessage(html=env.rich_html, media=media),
                reply_markup=None,
            )
        )
        return _classify_acceptance(msg)
    except TelegramForbiddenError as exc:
        return TelegramDeliveryResult(status="blocked", error_message=str(exc))
    except Exception as exc:
        logger.warning("telegram rich send failed (%s), falling back to photo/text", exc)
        return None


async def _send_photo_attempt(
    bot: Bot, env: TelegramDeliveryEnvelope
) -> TelegramDeliveryResult | None:
    from aiogram.types import BufferedInputFile

    raw = env.photo_bytes or (env.photos_bytes[0] if env.photos_bytes else None)
    photo = BufferedInputFile(raw, filename="listing.jpg") if raw else env.photo_input
    if photo is None:
        return None

    caption = env.text if len(env.text) <= 1024 else env.text[:1020] + "…"
    try:
        msg = await bot.send_photo(
            env.chat_id, photo, caption=caption, parse_mode=env.parse_mode, reply_markup=env.reply_markup
        )
        return _classify_acceptance(msg)
    except Exception as exc:
        return _classify_send_error(exc, "photo")


async def _send_text_attempt(bot: Bot, env: TelegramDeliveryEnvelope) -> TelegramDeliveryResult:
    try:
        msg = await bot.send_message(
            env.chat_id,
            env.text,
            disable_web_page_preview=False,
            parse_mode=env.parse_mode,
            reply_markup=env.reply_markup,
        )
        return _classify_acceptance(msg)
    except TelegramRetryAfter as exc:
        return TelegramDeliveryResult(
            status="retry", retry_after=exc.retry_after,
            error_message=str(exc), retry_scope="chat",
        )
    except TelegramForbiddenError as exc:
        return TelegramDeliveryResult(status="blocked", error_message=str(exc))
    except TelegramBadRequest as exc:
        if "can't parse entities" in str(exc).lower() and env.parse_mode:
            try:
                plain_text = re.sub(r"<[^>]+>", "", env.text)
                msg = await bot.send_message(
                    env.chat_id,
                    plain_text,
                    disable_web_page_preview=False,
                    parse_mode=None,
                    reply_markup=env.reply_markup,
                )
                return _classify_acceptance(msg)
            except Exception:
                pass
        return TelegramDeliveryResult(status=_classify_bad_request(exc), error_message=str(exc))
    except (TelegramNetworkError, TimeoutError, asyncio.TimeoutError) as exc:
        return TelegramDeliveryResult(status="ambiguous", retry_after=30, error_message=str(exc))
    except Exception as exc:
        return TelegramDeliveryResult(status="retry", retry_after=30, error_message=str(exc))


async def send_telegram_envelope(bot: Bot, env: TelegramDeliveryEnvelope) -> TelegramDeliveryResult:
    """Send delivery envelope via bot with rich/photo/text fallback and typed classification."""
    has_photo = bool(env.photos_bytes or env.photo_bytes or isinstance(env.photo_input, bytes))
    if env.rich_html and has_photo:
        res = await _send_rich_attempt(bot, env)
        if res is not None:
            return res
    if has_photo or env.photo_input:
        res = await _send_photo_attempt(bot, env)
        if res is not None:
            return res
    return await _send_text_attempt(bot, env)


def _resolve_photo_urls(payload: dict[str, Any], send_photos: bool) -> list[str]:
    """Extracts up to 3 high-res photo URLs from delivery payload."""
    if not send_photos or payload.get("disable_photo"):
        return []
    raw_list = payload.get("photos") or payload.get("image_urls")
    urls: list[str] = []
    if isinstance(raw_list, list):
        for u in raw_list:
            if isinstance(u, str) and u.startswith("http"):
                urls.append(re.sub(r"/\d+x\d+/", "/1280x960/", u))
    single = payload.get("image_url")
    if not urls and isinstance(single, str) and single.startswith("http"):
        urls.append(re.sub(r"/\d+x\d+/", "/1280x960/", single))
    return urls[:3]


def _resolve_photo_url(payload: dict[str, Any], send_photos: bool) -> str | None:
    urls = _resolve_photo_urls(payload, send_photos)
    return urls[0] if urls else None


class TelegramDeliveryAdapter:
    """Adapter bridging outbox ClaimedDelivery with Telegram sending."""

    def __init__(
        self, bot: Bot, *, send_photos: bool = True, photo_client: Any = None
    ) -> None:
        self.bot = bot
        self.send_photos = send_photos
        self.photo_client = photo_client

    async def send_delivery(self, delivery: ClaimedDelivery, destination: int | str) -> SendResult:
        p = delivery.payload
        photo_urls = _resolve_photo_urls(p, self.send_photos)
        photos_bytes = (
            await fetch_photos_bytes(photo_urls, client=self.photo_client)
            if photo_urls
            else []
        )
        time_ago = p.get("time_ago") or format_relative_time(p.get("published_at"))
        new_price, old_price = p.get("new_price", p.get("price")), p.get("old_price")
        loc, title = p.get("location"), p.get("title", "")
        dlv = bool(p["delivery"]) if "delivery" in p and p["delivery"] is not None else None
        resv = bool(p.get("is_reserved"))
        seller_line = p.get("seller_line") or format_seller_line(
            name=p.get("seller_name"), rating=p.get("seller_rating"),
            reviews=p.get("seller_reviews"), closed_items=p.get("closed_items"),
            seller_type=p.get("seller_type"), include_name=False,
        )
        desc = p.get("description") or p.get("description_preview") or p.get("snippet")
        q_name = p.get("search_name") or p.get("query_name") or p.get("reason")
        photo_ids = [f"p{i+1}" for i in range(len(photos_bytes))] if photos_bytes else None

        text = format_alert_caption(
            title=title, new_price=new_price, old_price=old_price,
            location=loc, time_ago=time_ago, is_delivery=dlv, is_reserved=resv,
            seller_line=seller_line, description=desc, search_name=q_name,
        )
        rich_html = format_rich_html(
            title=title, new_price=new_price, old_price=old_price,
            location=loc, time_ago=time_ago, is_delivery=dlv, is_reserved=resv,
            photo_ids=photo_ids, seller_line=seller_line, description=desc,
            search_name=q_name, avito_id=p.get("avito_id"), url=p.get("url"),
            date_time=p.get("published_at"),
        ) if photos_bytes else None

        env = TelegramDeliveryEnvelope(
            chat_id=destination, text=text, photo_input=photo_urls[0] if photo_urls else None,
            photo_bytes=photos_bytes[0] if photos_bytes else None, photos_bytes=photos_bytes,
            rich_html=rich_html, reply_markup=build_alert_keyboard(p.get("avito_id"), p.get("url")),
            parse_mode="HTML", delivery_id=delivery.id, event_id=delivery.event_id,
            user_id=delivery.user_id,
        )
        res = await send_telegram_envelope(self.bot, env)
        return res.to_send_result()
