"""Alert card formatting and keyboard builder for Telegram deliveries (S3.1 / S3.2)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import html
import re
from typing import Any
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

_AVITO_ITEM_ID_RE = re.compile(r"_(\d{8,14})(?:\?|$)")


def to_short_avito_url(url: str | None, avito_id: str | int | None = None) -> str:
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


def format_datetime_msk(dt_raw: str | datetime | None = None) -> str:
    """Formats timestamp into 'дд/мм/гггг, чч:мм' (MSK UTC+3)."""
    msk_tz = timezone(timedelta(hours=3))
    if not dt_raw:
        return datetime.now(msk_tz).strftime("%d/%m/%Y, %H:%M")
    if isinstance(dt_raw, datetime):
        dt = dt_raw if dt_raw.tzinfo else dt_raw.replace(tzinfo=timezone.utc)
        return dt.astimezone(msk_tz).strftime("%d/%m/%Y, %H:%M")
    try:
        dt = datetime.fromisoformat(str(dt_raw).replace("Z", "+00:00"))
        return dt.astimezone(msk_tz).strftime("%d/%m/%Y, %H:%M")
    except Exception:
        return datetime.now(msk_tz).strftime("%d/%m/%Y, %H:%M")


def format_price_rub(price: int | None) -> str:
    """Formats integer price with non-breaking thousand spaces and ruble sign."""
    if price is None:
        return "Цена не указана"
    return f"{price:,} ₽".replace(",", " ")


def format_relative_time(published_at: str | None, now_dt: datetime | None = None) -> str | None:
    """Calculates human-readable relative time from ISO publication timestamp."""
    if not published_at:
        return None
    try:
        dt = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        now = now_dt or datetime.now(timezone.utc)
        diff_sec = max(0, int((now - dt).total_seconds()))
        if diff_sec < 120:
            return "только что"
        if diff_sec < 3600:
            return f"{diff_sec // 60} мин назад"
        return f"{diff_sec // 3600} ч назад" if diff_sec < 86400 else f"{diff_sec // 86400} дн назад"
    except Exception:
        return None


def format_seller_line(
    name: str | None = None,
    rating: float | str | None = None,
    reviews: int | str | None = None,
    closed_items: int | str | None = None,
    seller_type: str | None = None,
    include_name: bool = True,
) -> str | None:
    """Formats seller trust line: 👤 name · ⭐ rating (reviews) · closed_items."""
    parts: list[str] = []
    if include_name:
        seller_name = (name or "").strip()
        if seller_name:
            parts.append(f"👤 {html.escape(seller_name, quote=False)}")
        elif seller_type:
            st_label = "Частное лицо" if seller_type.lower() in ("private", "user") else "Компания"
            parts.append(f"👤 {st_label}")

    rating_str = str(rating).strip().replace(".", ",") if rating is not None else ""
    reviews_str = str(reviews).strip() if reviews is not None else ""
    if rating_str and reviews_str:
        rev_text = reviews_str if "отз" in reviews_str else f"{reviews_str} отз."
        parts.append(f"⭐ {rating_str} ({rev_text})")
    elif rating_str:
        parts.append(f"⭐ {rating_str}")

    if closed_items is not None:
        c_str = str(closed_items).strip()
        parts.append(c_str if "заверш" in c_str else f"{c_str} завершённых")

    return " · ".join(parts) if parts else None


def _format_price_header(new_price: int | None, old_price: int | None = None) -> str:
    """Formats classic price header line."""
    if old_price is not None and new_price is not None and old_price > new_price:
        diff = old_price - new_price
        pct = round(diff / old_price * 100)
        return (
            f"📉 <b>Цена снизилась!</b>\n\n"
            f"<s>{format_price_rub(old_price)}</s> ➔ <b>{format_price_rub(new_price)}</b> "
            f"(−{format_price_rub(diff)} · −{pct}%)"
        )
    return f"<b>{format_price_rub(new_price)}</b>"


def _format_delivery_badge(is_delivery: bool | None) -> str:
    if is_delivery is True:
        return "🚚 Есть доставка"
    if is_delivery is False:
        return "🚚 Нет доставки"
    return "🚚 Доставка не проверена"


def format_alert_caption(
    title: str,
    new_price: int | None,
    old_price: int | None = None,
    location: str | None = None,
    time_ago: str | None = None,
    is_delivery: bool | None = None,
    is_reserved: bool | None = None,
    reason: str | None = None,
    seller_line: str | None = None,
    description: str | None = None,
    search_name: str | None = None,
    max_chars: int = 1000,
) -> str:
    """Builds clean, structured HTML caption for photo delivery or text fallback."""
    lines: list[str] = [_format_price_header(new_price, old_price)]

    badges: list[str] = [_format_delivery_badge(is_delivery)]
    if is_reserved:
        badges.append("🔒 В брони")
    lines.append(" · ".join(badges))

    clean_title = html.escape(title.strip(), quote=False)
    lines.append(f"🎧 <b>{clean_title}</b>")

    loc_time: list[str] = []
    if location:
        loc_time.append(f"📍 {html.escape(location.strip(), quote=False)}")
    if time_ago:
        loc_time.append(time_ago.strip())
    if loc_time:
        lines.append(" · ".join(loc_time))

    if seller_line and "⭐" in seller_line:
        lines.append(seller_line.strip())
    elif seller_line:
        lines.append(f"{seller_line.strip()} · ⭐ Нет отзывов")
    else:
        lines.append("⭐ Нет отзывов")

    if description:
        clean_desc = html.escape(description.strip(), quote=False)
        clean_desc = clean_desc[:297].rstrip() + "…" if len(clean_desc) > 300 else clean_desc
        lines.append(f"<blockquote>{clean_desc}</blockquote>")

    query_label = search_name or reason
    if query_label:
        lines.append(f"🎯 Поиск: «{html.escape(query_label.strip(), quote=False)}»")

    text = "\n\n".join(lines)
    if len(text) > max_chars:
        text = text[: max_chars - 1].rstrip() + "…"
    return text


def _build_rich_header(
    title: str,
    new_price: int | None,
    old_price: int | None = None,
    location: str | None = None,
    is_reserved: bool | None = None,
) -> list[str]:
    """Builds header paragraph, discount/reservation badges, and location."""
    if old_price is not None and new_price is not None and old_price > new_price:
        diff, pct = old_price - new_price, round((old_price - new_price) / old_price * 100)
        price_line = (
            f"<b>{format_price_rub(new_price)}</b> <s>{format_price_rub(old_price)}</s>\n"
            f'  <tg-button type="callback_data" data="noop" style="success">\n'
            f"    −{format_price_rub(diff)} (−{pct}%)\n"
            f"  </tg-button>"
        )
    else:
        price_line = f"<b>{format_price_rub(new_price)}</b>"

    loc_str = f"📍 {html.escape(location.strip(), quote=False)}" if location else ""
    if is_reserved:
        badge = '<p><br/><tg-button type="callback_data" data="noop" style="danger">🔒 Забронировано</tg-button><br/><br/></p>'
        res = [f"<p>\n  <b>{title}</b><br/>\n  {price_line}\n</p>", badge]
        if loc_str:
            res.append(f"<p>{loc_str}</p>")
        return res

    gap = f"<br/><br/>\n  {loc_str}" if loc_str else ""
    return [f"<p>\n  <b>{title}</b><br/>\n  {price_line}{gap}\n</p>"]


def format_rich_html(
    title: str,
    new_price: int | None,
    old_price: int | None = None,
    location: str | None = None,
    time_ago: str | None = None,
    is_delivery: bool | None = None,
    is_reserved: bool | None = None,
    photo_ids: list[str] | None = None,
    photo_id: str | None = None,
    seller_line: str | None = None,
    description: str | None = None,
    search_name: str | None = None,
    avito_id: str | int | None = None,
    url: str | None = None,
    date_time: str | datetime | None = None,
) -> str:
    """Formats Telegram Rich Message HTML according to approved production layout."""
    ids = photo_ids or ([photo_id] if photo_id else [])
    imgs = "".join(f'<img src="tg://photo?id={pid}"/>' for pid in ids if pid)
    blocks: list[str] = [f"<tg-slideshow>{imgs}</tg-slideshow>"] if imgs else []

    clean_title = html.escape(title.strip(), quote=False)
    blocks.extend(_build_rich_header(clean_title, new_price, old_price, location, is_reserved))

    if description:
        clean_desc = html.escape(description.strip(), quote=False)
        clean_desc = clean_desc[:297].rstrip() + "…" if len(clean_desc) > 300 else clean_desc
        blocks.append(f"<blockquote expandable>{clean_desc}</blockquote>")

    info: list[str] = [_format_delivery_badge(is_delivery)]
    s_clean = re.sub(r"^👤\s*[^·]+·\s*", "", seller_line.strip()) if seller_line else ""
    info.append(s_clean if (s_clean and "⭐" in s_clean) else "⭐ Нет отзывов")
    blocks.append(f"<p>\n  {'<br/>\n  '.join(info)}\n</p>")

    short_url = to_short_avito_url(url, avito_id=avito_id)
    if short_url:
        blocks.append(f"<p>🔗 {short_url}</p>")

    clean_search = html.escape(search_name.strip(), quote=False) if search_name else "Поиск"
    blocks.append("<p><br/></p>")
    blocks.append(f"<footer>🎯 {clean_search} · {format_datetime_msk(date_time)}</footer>")

    return "\n\n".join(blocks)


def build_alert_keyboard(
    avito_id: str | int | None,
    url: str | None = None,
) -> InlineKeyboardMarkup:
    """Builds single primary action button under the listing alert."""
    aid = str(avito_id).strip() if avito_id is not None else ""
    short_url = f"https://www.avito.ru/{aid}" if aid.isdigit() and len(aid) >= 8 else (url or "https://www.avito.ru")
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔗 Открыть на Авито", url=short_url)]]
    )
