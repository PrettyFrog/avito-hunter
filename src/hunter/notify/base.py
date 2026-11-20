from __future__ import annotations

from loguru import logger

from hunter.core.models import NotificationEvent
from hunter.notify.base_parts.format_event_01 import (
    _append_meta_lines,
    _append_reason_lines,
    _build_model_line,
)


class Channel:
    name = "base"

    async def send(self, event: NotificationEvent) -> bool:
        raise NotImplementedError

    async def alert(self, text: str) -> bool:
        raise NotImplementedError


class Notifier:
    def __init__(self, channels: list[Channel]) -> None:
        self.channels = channels
        self.pending_alerts: list[str] = []

    async def send(self, event: NotificationEvent) -> str | None:
        for channel in self.channels:
            try:
                if await channel.send(event):
                    return channel.name
            except Exception:
                logger.exception("channel {} failed", channel.name)
        return None

    async def alert(self, text: str) -> bool:
        delivered = False
        for channel in self.channels:
            try:
                if await channel.alert(text):
                    delivered = True
                    break
            except Exception:
                logger.exception("alert via {} failed", channel.name)
        if not delivered:
            if text in self.pending_alerts:
                logger.debug("Duplicate alert already in pending_alerts: {}", text)
                return False
            if len(self.pending_alerts) >= 50:
                dropped = self.pending_alerts.pop(0)
                logger.error("pending_alerts overflow (>50): dropped oldest alert: {}", dropped)
            logger.error("Alert delivery failed, queued in pending_alerts: {}", text)
            self.pending_alerts.append(text)
            return False
        await self.flush_alerts()
        return True

    async def flush_alerts(self) -> None:
        if not self.pending_alerts:
            return
        remaining: list[str] = []
        for pending in self.pending_alerts:
            delivered = False
            for channel in self.channels:
                try:
                    if await channel.alert(pending):
                        delivered = True
                        break
                except Exception:
                    pass
            if not delivered:
                remaining.append(pending)
        self.pending_alerts = remaining

    async def listen(self) -> None:
        for channel in self.channels:
            listen = getattr(channel, "listen", None)
            if listen is None:
                continue
            await listen()


def format_event(event: NotificationEvent) -> str:
    is_gem = (
        getattr(event, "is_gem_feed", False)
        or event.watchlist_id.startswith("gem_")
        or any(r.startswith("gem:") for r in event.reasons)
    )
    header = (
        f"🔥 [СКРЫТАЯ ЖЕМЧУЖИНА] {event.category}"
        if is_gem
        else f"{event.category} / {event.watchlist_id}"
    )
    lines = [
        header,
        event.title,
        f"цена {event.price} ₽  (потолок {event.max_price} ₽)",
    ]
    if getattr(event, "price_reduced_from_above_max", None):
        old_p, new_p = event.price_reduced_from_above_max
        lines.insert(0, f"цена снижена с {old_p} до {new_p} ₽ (была выше потолка)")

    model_line = _build_model_line(event, is_gem)
    if model_line:
        lines.append(model_line)

    _append_meta_lines(lines, event)
    _append_reason_lines(lines, event)

    if event.checklist:
        lines.append("📋 Чек-лист: " + ", ".join(event.checklist[:3]))
    lines.append(event.url)
    return "\n".join(lines)
