"""Durable sequence allocation shared by all public polling workers."""

from __future__ import annotations

from typing import Any


class PollSequenceAllocator:
    """Allocates a global monotone request sequence through the SQLite writer."""

    def __init__(self, writer: Any, key: str = "poll_sequence:global") -> None:
        self.writer = writer
        self.key = key

    async def next_sequence(self) -> int:
        async def _tx(conn: Any) -> int:
            row = await (await conn.execute(
                "SELECT value FROM runtime WHERE key = ?", (self.key,)
            )).fetchone()
            current = int(row[0]) if row else await self._existing_high_water(conn)
            sequence = current + 1
            await conn.execute(
                "INSERT INTO runtime (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (self.key, str(sequence)),
            )
            return sequence

        return await self.writer.submit(_tx)

    async def _existing_high_water(self, conn: Any) -> int:
        row = await (await conn.execute(
            "SELECT COALESCE(MAX(last_request_sequence), 0) FROM listing_state"
        )).fetchone()
        return int(row[0]) if row else 0
