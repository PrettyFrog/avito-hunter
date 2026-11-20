from __future__ import annotations

import httpx
from loguru import logger

from hunter.core.config import get_settings
from hunter.core.models import NotificationEvent
from hunter.notify.base import Channel, format_event


class NtfyChannel(Channel):
    name = "ntfy"

    def enabled(self) -> bool:
        return bool(get_settings().ntfy_url)

    async def send(self, event: NotificationEvent) -> bool:
        return await self.alert(format_event(event))

    async def alert(self, text: str) -> bool:
        url = get_settings().ntfy_url
        if not url:
            return False
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(url, content=text.encode("utf-8"))
            response.raise_for_status()
        logger.info("ntfy ok")
        return True
