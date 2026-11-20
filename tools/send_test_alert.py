"""CLI tool to verify alert card rendering directly in Telegram (S3/S6)."""

from __future__ import annotations

import argparse
import asyncio
import logging
from typing import Any

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession

from hunter.core.settings import Settings
from hunter.notify.telegram_alert_card import (
    build_alert_keyboard,
    format_alert_caption,
    format_rich_html,
    format_seller_line,
)
from hunter.notify.telegram_delivery import (
    TelegramDeliveryEnvelope,
    fetch_photos_bytes,
    send_telegram_envelope,
)

logger = logging.getLogger(__name__)


def _build_envelope(
    chat_id: int | str,
    title: str,
    new_price: int,
    old_price: int | None,
    location: str,
    is_delivery: bool,
    is_reserved: bool,
    seller_name: str | None,
    seller_rating: float | None,
    seller_reviews: str | None,
    closed_items: str | None,
    description: str | None,
    search_name: str | None,
    avito_id: str,
    p_bytes: list[bytes],
) -> TelegramDeliveryEnvelope:
    seller_line = format_seller_line(
        name=seller_name,
        rating=seller_rating,
        reviews=seller_reviews,
        closed_items=closed_items,
    )
    photo_ids = [f"p{i+1}" for i in range(len(p_bytes))] if p_bytes else None

    caption = format_alert_caption(
        title=title, new_price=new_price, old_price=old_price,
        location=location, time_ago="только что",
        is_delivery=is_delivery, is_reserved=is_reserved,
        seller_line=seller_line, description=description,
        search_name=search_name,
    )
    rich = format_rich_html(
        title=title, new_price=new_price, old_price=old_price,
        location=location, time_ago="только что",
        is_delivery=is_delivery, is_reserved=is_reserved,
        photo_ids=photo_ids, seller_line=seller_line,
        description=description, search_name=search_name,
    ) if p_bytes else None

    return TelegramDeliveryEnvelope(
        chat_id=chat_id,
        text=caption,
        photo_bytes=p_bytes[0] if p_bytes else None,
        photos_bytes=p_bytes,
        rich_html=rich,
        reply_markup=build_alert_keyboard(avito_id),
        parse_mode="HTML",
    )


async def send_verification_alert(
    token: str,
    chat_id: int | str,
    proxy: str | None = None,
    title: str = "iPhone 16, 128 ГБ, SIM + eSIM",
    new_price: int = 60000,
    old_price: int | None = None,
    location: str = "Москва, м. Аннино",
    is_delivery: bool = True,
    is_reserved: bool = False,
    seller_name: str = "Александр",
    seller_rating: float = 4.9,
    seller_reviews: str = "59 отзывов",
    closed_items: str = "24 завершённых",
    description: str = "IPhone 16, 128 ГБ, Sim + esim. Новое оригинальное устройство. Не вскрывалось.",
    search_name: str = "iPhone 16",
    avito_id: str = "8440828261",
    photo_urls: list[str] | None = None,
) -> Any:
    urls = photo_urls or [
        "https://c90.img.avito.st/image/1/1.L-deF7aAgw4QvXkFDBN741-3gQrsoIcO7MfiCuxMiPThtIcUEL15Beg.P0dXmQAOIRDmT4RhgH98eKjMr5qGbVrHrKUdyenncaU",
        "https://c70.img.avito.st/image/1/1.93w-pLaAW5VwDqGeXN-OGCwEWZGME1-VjHQ6kYz_UG-BB1-PcA6hnog.nJFv5rCa9JgZp16f5KaV2r25REy9nnEaItyKfeHy4Jw",
    ]
    p_bytes = await fetch_photos_bytes(urls)
    env = _build_envelope(
        chat_id=chat_id, title=title, new_price=new_price, old_price=old_price,
        location=location, is_delivery=is_delivery, is_reserved=is_reserved,
        seller_name=seller_name, seller_rating=seller_rating,
        seller_reviews=seller_reviews, closed_items=closed_items,
        description=description, search_name=search_name, avito_id=avito_id,
        p_bytes=p_bytes,
    )
    session = AiohttpSession(proxy=proxy) if proxy else None
    bot = Bot(token=token, session=session)
    try:
        return await send_telegram_envelope(bot, env)
    finally:
        await bot.session.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Send test alert to Telegram")
    parser.add_argument("--chat-id", type=int, default=1080079470)
    parser.add_argument("--drop", action="store_true", help="Simulate price drop")
    args = parser.parse_args()

    settings = Settings()
    token = settings.telegram_bot_token
    proxy = settings.http_proxy
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN not configured")

    if args.drop:
        res = asyncio.run(send_verification_alert(
            token=token, chat_id=args.chat_id, proxy=proxy,
            title="Оперативная память DDR4 16GB", new_price=3200, old_price=4000,
            location="Санкт-Петербург, Центральный", is_delivery=False,
            seller_name="Иван", seller_rating=5.0, seller_reviews="12 отзывов",
            closed_items="15 завершённых",
            description="Продаю оперативную память DDR4 16GB в отличном состоянии.",
            search_name="DDR4", avito_id="8455454113",
        ))
    else:
        res = asyncio.run(send_verification_alert(token=token, chat_id=args.chat_id, proxy=proxy))
    print("Sent test alert:", res)


if __name__ == "__main__":
    main()
