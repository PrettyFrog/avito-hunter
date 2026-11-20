"""VK messenger backup notification channel using VK Community API."""

from __future__ import annotations

import os
import httpx
from loguru import logger

from hunter.core.config import get_settings
from hunter.core.models import NotificationEvent
from hunter.notify.vk_parts.vknotifier_send_notification_01 import (
    post_vk_message,
    prepare_vk_payload,
)

__all__ = ["VkNotifier"]


class VkNotifier:
    """Sends notifications via VK API messages.send on behalf of a community."""

    def __init__(
        self,
        token: str | None = None,
        user_id: int | str | None = None,
        mode: str | None = None,
        version: str = "5.199",
    ) -> None:
        settings = get_settings()
        vk_cfg = getattr(settings, "notify", {}).get("vk", {}) if hasattr(settings, "notify") else {}
        self.token = token or os.getenv("VK_GROUP_TOKEN")
        raw_uid = user_id or os.getenv("VK_USER_ID")
        self.user_id = int(raw_uid) if raw_uid and str(raw_uid).isdigit() else None
        self.mode = mode or vk_cfg.get("mode", "fallback")
        self.version = version or vk_cfg.get("version", "5.199")
        if self.enabled:
            logger.info("VkNotifier initialized (user_id={}, mode={})", self.user_id, self.mode)
        else:
            logger.debug("VkNotifier disabled (no token or user_id)")

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.user_id)

    def format_message(self, event: NotificationEvent) -> str:
        """Format notification: model, price, ceiling, age, link, flags, no buttons."""
        lines = []
        if getattr(event, "price_reduced_from_above_max", None):
            old_p, new_p = event.price_reduced_from_above_max
            lines.append(f"цена снижена с {old_p} до {new_p} ₽ (была выше потолка)")

        model_name = event.model_id or event.watchlist_id
        lines.append(f"{event.category} / {model_name}")
        lines.append(event.title)
        lines.append(f"цена: {event.price:,} ₽ (потолок {event.max_price:,} ₽)".replace(",", " "))

        age_text = getattr(event, "age_text", None)
        if age_text:
            lines.append(f"возраст: {age_text}")

        for r in event.reasons:
            if "по фото похоже на другую модель" in r:
                lines.append(r)
            elif r.startswith("gem_reason:"):
                lines.append(f"💡 {r[len('gem_reason:'):]}")
            elif r.startswith("red_flag:"):
                lines.append(f"🚩 {r}")

        lines.append(event.url)
        return "\n".join(lines)

    async def send_notification(
        self,
        event: NotificationEvent,
        client: httpx.AsyncClient | None = None,
        db=None,
        telegram_notifier=None,
        recipient_user_id: int | None = None,
    ) -> bool:
        """Send message via messages.send. Returns True on success, False otherwise."""
        if not self.enabled:
            return False

        effective_uid, data = await prepare_vk_payload(
            self, event, db, recipient_user_id=recipient_user_id
        )
        if not effective_uid or not data:
            return False

        recipient_uid = (
            recipient_user_id
            if recipient_user_id is not None
            else getattr(event, "recipient_user_id", None)
        )
        should_close = False
        if client is None:
            client = httpx.AsyncClient(timeout=10.0)
            should_close = True

        try:
            return await post_vk_message(
                client=client,
                data=data,
                effective_uid=effective_uid,
                recipient_uid=recipient_uid,
                event=event,
                db=db,
                telegram_notifier=telegram_notifier,
            )
        finally:
            if should_close:
                await client.aclose()
