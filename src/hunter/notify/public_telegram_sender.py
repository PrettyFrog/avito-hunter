"""Verified tenant Telegram delivery for the public v2 outbox."""

from __future__ import annotations

from typing import Any

from hunter.application.dispatcher import SendResult
from hunter.notify.digest_delivery import DigestChunk, format_digest_chunks
from hunter.notify.telegram_delivery import (
    TelegramDeliveryAdapter,
    TelegramDeliveryEnvelope,
    send_telegram_envelope,
)
from hunter.storage.deliveries import ClaimedDelivery


class PublicTelegramSender:
    """Resolve a current verified destination before sending each leased intent."""

    def __init__(
        self,
        bootstrap: Any,
        channel: Any,
        photo_client: Any = None,
        heartbeat_interval: float = 10.0,
    ) -> None:
        self.bootstrap = bootstrap
        self.channel = channel
        self._photo_client = photo_client
        self.heartbeat_interval = heartbeat_interval
        self._heartbeat_task: Any = None

    def start_heartbeat(self) -> None:
        """Start background keep-alive ping loop to keep Telegram API socket hot."""
        if self._heartbeat_task is None and self.heartbeat_interval > 0:
            import asyncio

            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def _heartbeat_loop(self) -> None:
        import asyncio

        while True:
            try:
                await asyncio.sleep(self.heartbeat_interval)
                bot = await self.channel._client()
                if hasattr(bot, "get_me"):
                    await bot.get_me()
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    def _get_photo_client(self) -> Any:
        if self._photo_client is None:
            import httpx

            limits = httpx.Limits(
                max_keepalive_connections=5, max_connections=10, keepalive_expiry=120.0
            )
            self._photo_client = httpx.AsyncClient(timeout=4.0, limits=limits, trust_env=False)
        return self._photo_client

    async def aclose(self) -> None:
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None
        if self._photo_client is not None:
            if hasattr(self._photo_client, "aclose"):
                await self._photo_client.aclose()
            self._photo_client = None

    async def _destination(self, delivery: ClaimedDelivery) -> str | None:
        reader = await self.bootstrap.conn_mgr.open_read_connection()
        try:
            row = await (await reader.execute(
                "SELECT address, version FROM channel_destinations "
                "WHERE user_id = ? AND channel = 'telegram' AND verified = 1",
                (delivery.user_id,),
            )).fetchone()
            if not row or int(row[1]) != delivery.destination_version:
                return None
            return str(row[0])
        finally:
            await self.bootstrap.conn_mgr.close_reader(reader)

    async def __call__(self, delivery: ClaimedDelivery) -> SendResult:
        if delivery.channel != "telegram":
            return SendResult("dead_letter", error_message="Unsupported Telegram channel")
        destination = await self._destination(delivery)
        if destination is None:
            return SendResult(
                'dead_'
                'letter', error_message="No matching verified Telegram destination")
        bot = await self.channel._client()
        adapter = TelegramDeliveryAdapter(bot, photo_client=self._get_photo_client())
        return await adapter.send_delivery(delivery, destination)

    async def send_digest(
        self, deliveries: list[ClaimedDelivery], chunk: DigestChunk
    ) -> SendResult:
        try:
            expected = format_digest_chunks(deliveries)
        except (TypeError, ValueError):
            return SendResult("dead_letter", error_message="Invalid digest contents")
        if len(expected) != 1 or expected[0] != chunk:
            return SendResult("dead_letter", error_message="Digest contents do not match")
        destination = await self._destination(deliveries[0])
        if destination is None:
            return SendResult("dead_letter", error_message="No matching verified Telegram destination")
        bot = await self.channel._client()
        result = await send_telegram_envelope(
            bot, TelegramDeliveryEnvelope(chat_id=destination, text=chunk.text)
        )
        return result.to_send_result()
