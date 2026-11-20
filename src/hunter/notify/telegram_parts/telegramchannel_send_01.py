"""TelegramChannel send helper functions (part 1)."""

from __future__ import annotations

from typing import Any

from loguru import logger


def prepare_notification_payload(
    channel: Any,
    event: Any,
    target_chat_id: Any,
    format_user_alert_fn: Any,
    user_alert_keyboard_fn: Any,
    format_event_fn: Any,
) -> tuple[str, Any]:
    is_owner = str(target_chat_id) == str(channel.chat_id)
    if not is_owner:
        text = format_user_alert_fn(event)
        markup = user_alert_keyboard_fn(
            event.avito_id, url=event.url, product_id=event.watchlist_id
        )
    else:
        text = format_event_fn(event)
        markup = channel._keyboard(event.avito_id, url=event.url, product_id=event.watchlist_id)
    return text, markup


async def resolve_photo_input(
    event: Any,
    settings: Any,
) -> Any:
    collage_img = settings.images_dir / event.avito_id / "collage.jpg"
    local_img = settings.images_dir / event.avito_id / "img_0.jpg"
    target_photo = (
        collage_img if (collage_img.exists() and collage_img.stat().st_size > 0) else local_img
    )
    if target_photo.exists() and target_photo.stat().st_size > 0:
        from aiogram.types import FSInputFile

        return FSInputFile(str(target_photo))
    if event.image_url:
        try:
            import httpx
            from aiogram.types import BufferedInputFile

            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
                ),
                "Referer": "https://www.avito.ru/",
            }
            async with httpx.AsyncClient(
                timeout=10, headers=headers, proxy=settings.http_proxy
            ) as client:
                resp = await client.get(event.image_url)
                if resp.status_code == 200 and resp.content:
                    return BufferedInputFile(resp.content, filename="photo.jpg")
        except Exception as exc:
            logger.debug("downloading image for telegram failed: {}", exc)
    return None


async def handle_telegram_delivery_failure(
    channel: Any,
    event: Any,
    target_chat_id: Any,
    settings: Any,
    exc: Exception,
) -> None:
    from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

    # Owner is strictly exempt from being paused/blocked
    if str(target_chat_id) == str(channel.chat_id):
        logger.error("telegram send failed to owner {}: {}", channel.chat_id, exc)
        return

    is_blocked = isinstance(exc, TelegramForbiddenError) or (
        isinstance(exc, TelegramBadRequest)
        and any(term in str(exc).lower() for term in ("chat not found", "deactivated", "blocked"))
    )
    if is_blocked and event.recipient_user_id:
        logger.warning("User {} blocked bot or chat unavailable: {}", event.recipient_user_id, exc)
        from hunter.core.db import get_db
        from hunter.core.product import get_product_store, save_product_profile
        from hunter.notify.messages_ru import BOT_BLOCKED_OWNER_ALERT_TEMPLATE

        db = get_db()
        await db.set_user_status(event.recipient_user_id, "blocked")
        await db.log_user_event(event.recipient_user_id, "bot_blocked", f"Delivery failed: {exc}")

        store = get_product_store()
        profiles, _ = store.load_all()
        paused_count = 0
        for p in profiles.values():
            if p.owner_user_id == event.recipient_user_id and p.mode != "off":
                p.mode = "off"
                p.paused_reason = "bot_blocked"
                save_product_profile(p, directory=settings.data_dir / "products")
                paused_count += 1

        user = await db.get_user_by_id(event.recipient_user_id)
        username = (
            user.get("username")
            if isinstance(user, dict)
            else (getattr(user, "username", None) if user else None)
        )
        user_repr = f"@{username}" if username else f"ID {event.recipient_user_id}"
        alert_text = BOT_BLOCKED_OWNER_ALERT_TEMPLATE.format(user_repr=user_repr)
        await channel.alert(alert_text)


async def dispatch_telegram_envelope(
    channel: Any,
    bot: Any,
    event: Any,
    target_chat_id: Any,
    text: str,
    markup: Any,
    photo_input: Any,
    settings: Any,
) -> bool:
    from hunter.notify.telegram_delivery import TelegramDeliveryEnvelope, send_telegram_envelope

    env = TelegramDeliveryEnvelope(
        chat_id=target_chat_id,
        text=text,
        photo_input=photo_input,
        reply_markup=markup,
        user_id=event.recipient_user_id or 0,
    )
    res = await send_telegram_envelope(bot, env)
    channel.last_delivery_result = res
    if res.status == "sent":
        return True
    if res.status == "blocked":
        await handle_telegram_delivery_failure(
            channel,
            event=event,
            target_chat_id=target_chat_id,
            settings=settings,
            exc=Exception(res.error_message),
        )
    return False
