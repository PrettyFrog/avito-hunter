"""Consume private VK messages containing short-lived binding tokens."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from hunter.application.channel_binding import BindingError
from hunter.storage.writer import SQLiteWriterActor
from hunter.transport.vk_longpoll import VkLongPollClient

logger = logging.getLogger(__name__)


class VkBindingListener:
    """Bind a VK private peer through the durable single-writer transaction."""

    def __init__(
        self,
        group_id: int,
        writer: SQLiteWriterActor,
        binding_service: Any,
        client: VkLongPollClient,
    ) -> None:
        self.group_id = group_id
        self.writer = writer
        self.binding_service = binding_service
        self.client = client

    async def handle_update(self, update: dict[str, Any]) -> bool:
        message = _extract_private_message(update, self.group_id)
        if not message:
            return False
        peer_id, text = message
        token = self.binding_service.extract_token(text)
        if not token:
            return False
        try:
            await self.writer.submit(
                lambda conn: self.binding_service.bind_from_peer(conn, peer_id, token)
            )
        except BindingError:
            return False
        return True

    async def poll_once(self) -> int:
        batch = await self.client.poll()
        handled = 0
        for update in batch.updates:
            handled += int(await self.handle_update(update))
        self.client.ack(batch)
        return handled

    async def run(self, stop_event: asyncio.Event) -> None:
        try:
            await self._run_loop(stop_event)
        finally:
            await self.client.close()

    async def _run_loop(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("VK binding poll failed; retrying without logging credentials")
                await _wait_or_stop(stop_event, 2.0)


def _extract_private_message(update: dict[str, Any], group_id: int) -> tuple[int, str] | None:
    if update.get("type") != "message_new":
        return None
    try:
        if int(update.get("group_id", group_id)) != group_id:
            return None
    except (TypeError, ValueError):
        return None
    body = update.get("object")
    message = body.get("message", body) if isinstance(body, dict) else None
    if not isinstance(message, dict):
        return None
    try:
        sender_id = int(message.get("from_id", 0))
        peer_id = int(message.get("peer_id", 0))
    except (TypeError, ValueError):
        return None
    if sender_id <= 0 or peer_id != sender_id or peer_id >= 2_000_000_000:
        return None
    return peer_id, str(message.get("text", ""))


async def _wait_or_stop(stop_event: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except TimeoutError:
        pass
