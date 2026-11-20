"""Deliver VK intents only to the matching verified tenant destination."""

from hunter.application.dispatcher import SendResult
from hunter.notify.vk_delivery import VkDeliveryAdapter
from hunter.storage.connection import StorageConnectionManager
from hunter.storage.deliveries import ClaimedDelivery
from hunter.storage.writer import SQLiteWriterActor


class PublicVkSender:
    def __init__(
        self, connections: StorageConnectionManager, adapter: VkDeliveryAdapter,
        writer: SQLiteWriterActor | None = None,
    ) -> None:
        self.connections = connections
        self.adapter = adapter
        self.writer = writer

    async def __call__(self, delivery: ClaimedDelivery) -> SendResult:
        if delivery.channel != "vk":
            return SendResult("dead_letter", error_message="Unsupported VK channel")
        reader = await self.connections.open_read_connection()
        try:
            row = await (
                await reader.execute(
                    "SELECT address, version FROM channel_destinations "
                    "WHERE user_id = ? AND channel = 'vk' AND verified = 1",
                    (delivery.user_id,),
                )
            ).fetchone()
        finally:
            await self.connections.close_reader(reader)
        if not row or int(row[1]) != delivery.destination_version:
            return SendResult("dead_letter", error_message="No matching verified VK destination")
        address = str(row[0])
        if not address.isascii() or not address.isdigit() or not 0 < int(address) < 2_000_000_000:
            return SendResult("dead_letter", error_message="Invalid VK destination")
        result = await self.adapter.send_delivery(delivery, int(address))
        if result.recipient_blocked:
            await self._revoke_blocked(delivery, address)
        return result

    async def _revoke_blocked(self, delivery: ClaimedDelivery, address: str) -> None:
        if self.writer is None:
            raise RuntimeError("VK blocked destination cannot be revoked without writer")

        async def revoke(conn):
            await conn.execute(
                "UPDATE channel_destinations SET verified = 0 WHERE user_id = ? "
                "AND channel = 'vk' AND version = ? AND address = ? AND verified = 1",
                (delivery.user_id, delivery.destination_version, address),
            )

        await self.writer.submit(revoke)
