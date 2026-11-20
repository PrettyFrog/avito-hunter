"""Per-egress/upstream cooldown persisted exclusively by the owned SQLite writer."""
from dataclasses import dataclass
from hashlib import sha256
import json
import math
import time
from typing import Any


@dataclass(frozen=True)
class CooldownRecord:
    generation: int
    until_utc: float
    saved_utc: float
    remaining_sec: float

    def monotonic_deadline(self, *, utc_now: float, mono_now: float) -> float:
        # A backwards wall-clock step must never shorten the original prohibition.
        remaining = self.until_utc - utc_now
        if utc_now < self.saved_utc:
            remaining = max(remaining, self.remaining_sec)
        return mono_now + max(0.0, remaining)


class RequestCooldownStore:
    """No timers, filesystem or network work occurs inside writer transactions."""

    def __init__(self, writer: Any, *, wall_clock=time.time):
        self.writer, self.wall_clock = writer, wall_clock

    @staticmethod
    def _key(egress_group: str | None, upstream: str) -> str:
        if not upstream:
            raise ValueError("Explicit upstream required")
        opaque = sha256(json.dumps([egress_group or "unknown", upstream]).encode()).hexdigest()
        return f"request:cooldown:{opaque}"

    async def extend(self, egress_group: str | None, upstream: str, *,
                     generation: int, duration_sec: float, shared_egress: bool = False) -> CooldownRecord:
        if type(generation) is not int or generation < 0 or not math.isfinite(duration_sec) or duration_sec < 0:
            raise ValueError("Invalid cooldown update")
        now = self.wall_clock()
        until = now + duration_sec
        if not math.isfinite(until):
            raise ValueError("Unrepresentable cooldown deadline")
        key = self._key(egress_group, upstream)
        async def transaction(conn):
            cursor = await conn.execute("SELECT value FROM runtime WHERE key = ?", (key,))
            row = await cursor.fetchone()
            previous = CooldownRecord(**json.loads(row[0])) if row else None
            if previous and generation < previous.generation and not shared_egress:
                return previous
            deadline = max(until, previous.until_utc if previous else until)
            record = CooldownRecord(max(generation, previous.generation if previous else generation),
                                    deadline, now, max(0.0, deadline - now))
            await conn.execute(
                "INSERT INTO runtime(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(record.__dict__)),
            )
            return record
        return await self.writer.submit(transaction)

    async def load(self, egress_group: str | None, upstream: str) -> CooldownRecord | None:
        key = self._key(egress_group, upstream)
        async def read(conn):
            cursor = await conn.execute("SELECT value FROM runtime WHERE key = ?", (key,))
            row = await cursor.fetchone()
            return CooldownRecord(**json.loads(row[0])) if row else None
        return await self.writer.submit(read)
