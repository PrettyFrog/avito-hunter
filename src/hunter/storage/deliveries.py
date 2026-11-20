"""Outbox delivery repository: conditional leases, due index, retry, \
and dead letter (S3/S6 / T-484)."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any

import aiosqlite

from hunter.storage.connection import StorageConnectionManager
from hunter.storage.writer import SQLiteWriterActor

logger = logging.getLogger(__name__)


class FailureKind(str, Enum):
    TRANSIENT = "transient"
    PERMANENT = "permanent"
    AMBIGUOUS = "ambiguous"
    MALFORMED = "malformed"


@dataclass(frozen=True, slots=True)
class ClaimedDelivery:
    id: str
    event_id: str
    user_id: int
    channel: str
    payload_version: int
    payload: dict
    attempt: int
    lease_token: str
    lease_until: str
    destination_version: int = 1
    created_at: str = ""


class DeliveryRepository:
    """Manages outbox deliveries with conditional leases and retry policies."""

    def __init__(self, mgr: StorageConnectionManager, writer: SQLiteWriterActor) -> None:
        self.mgr = mgr
        self.writer = writer

    async def get_user_status(self, user_id: int) -> str | None:
        reader = await self.mgr.open_read_connection()
        try:
            row = await (await reader.execute(
                "SELECT status FROM users WHERE id = ?", (user_id,)
            )).fetchone()
            return str(row[0]) if row else None
        finally:
            await self.mgr.close_reader(reader)

    async def is_user_active(self, user_id: int) -> bool:
        return (await self.get_user_status(user_id)) == "active"

    async def purge_deleted_user_deliveries(self) -> int:
        async def _tx(c: aiosqlite.Connection) -> int:
            cur = await c.execute(
                "UPDATE deliveries SET status = 'dead_letter', lease_token = NULL, "
                "lease_until = NULL WHERE status IN ('pending', 'leased') AND user_id IN ("
                "SELECT id FROM users WHERE status IN ('deleted', 'blocked'))"
            )
            await c.execute(
                "UPDATE deliveries SET status = 'expired', lease_token = NULL, lease_until = NULL "
                "WHERE status IN ('pending', 'leased') AND "
                "datetime(replace(created_at, 'T', ' ')) < datetime('now', '-2 hours')"
            )
            await c.execute(
                "DELETE FROM runtime WHERE key LIKE 'delivery:telegram:chat:%' AND "
                "datetime(replace(value, 'T', ' ')) > datetime('now', '+10 minutes')"
            )
            return cur.rowcount

        return await self.writer.submit(_tx)

    async def claim_due(
        self,
        now_ts: str,
        limit: int = 10,
        lease_seconds: int = 30,
        exclude_telegram: bool = False,
    ) -> list[ClaimedDelivery]:
        token = uuid.uuid4().hex
        lease_until = (
            datetime.fromisoformat(now_ts.replace("Z", "+00:00")) + timedelta(seconds=lease_seconds)
        ).isoformat()

        return await self.writer.submit(
            lambda c: self._claim_due_tx(c, now_ts, limit, token, lease_until, exclude_telegram)
        )

    async def _claim_due_tx(
        self,
        c: aiosqlite.Connection,
        now_ts: str,
        limit: int,
        token: str,
        lease_until: str,
        exclude_telegram: bool = False,
    ) -> list[ClaimedDelivery]:
        sql = (
            "SELECT id, event_id, user_id, channel, payload_version, payload, "
            "attempt, destination_version, created_at "
            "FROM deliveries WHERE ((status = 'pending' AND available_at <= ?) "
            "OR (status = 'leased' AND lease_until < ?)) "
            "AND (? = 0 OR channel <> 'telegram') ORDER BY available_at ASC LIMIT ?"
        )
        rows = await (await c.execute(sql, (now_ts, now_ts, exclude_telegram, limit))).fetchall()
        claimed: list[ClaimedDelivery] = []
        for r in rows:
            delivery = await self._claim_delivery_row(c, r, now_ts, token, lease_until)
            if delivery is not None:
                claimed.append(delivery)
        return claimed

    async def _claim_delivery_row(
        self,
        c: aiosqlite.Connection,
        row: Any,
        now_ts: str,
        token: str,
        lease_until: str,
    ) -> ClaimedDelivery | None:
        did, eid, uid, ch, pver, praw, att = (
            row[0],
            row[1],
            int(row[2]),
            row[3],
            int(row[4]),
            row[5],
            int(row[6]),
        )
        destination_version = int(row[7])
        created_at = str(row[8] or "")
        try:
            if pver != 1:
                raise ValueError(f"Unsupported payload version {pver}")
            parsed = json.loads(praw)
        except Exception:
            await c.execute(
                "UPDATE deliveries SET status = 'dead_letter', lease_token = NULL, "
                "lease_until = NULL WHERE id = ?",
                (did,),
            )
            return None

        upd = await c.execute(
            "UPDATE deliveries SET status = 'leased', lease_token = ?, lease_until = ?, "
            "attempt = attempt + 1 "
            "WHERE id = ? AND ((status = 'pending' AND available_at <= ?) "
            "OR (status = 'leased' AND lease_until < ?))",
            (token, lease_until, did, now_ts, now_ts),
        )
        if upd.rowcount == 0:
            return None
        return ClaimedDelivery(
            did,
            eid,
            uid,
            ch,
            pver,
            parsed,
            att + 1,
            token,
            lease_until,
            destination_version,
            created_at,
        )

    async def renew_lease(
        self, delivery_id: str, lease_token: str, extend_seconds: int = 30
    ) -> bool:
        now_dt = datetime.now(UTC)
        new_until = (now_dt + timedelta(seconds=extend_seconds)).isoformat()

        async def _tx(c: aiosqlite.Connection) -> bool:
            cur = await c.execute(
                "UPDATE deliveries SET lease_until = ? WHERE id = ? "
                "AND status = 'leased' AND lease_token = ? AND lease_until >= ?",
                (new_until, delivery_id, lease_token, now_dt.isoformat()),
            )
            return cur.rowcount > 0

        return await self.writer.submit(_tx)

    async def complete(self, delivery_id: str, lease_token: str, message_id: str = "") -> bool:
        async def _tx(c: aiosqlite.Connection) -> bool:
            cur = await c.execute(
                "UPDATE deliveries SET status = 'sent', message_id = ?, "
                "lease_token = NULL, lease_until = NULL "
                "WHERE id = ? AND status = 'leased' AND lease_token = ?",
                (message_id, delivery_id, lease_token),
            )
            if cur.rowcount > 0:
                row = await (await c.execute(
                    "SELECT event_id, user_id FROM deliveries WHERE id = ?",
                    (delivery_id,),
                )).fetchone()
                if row:
                    await c.execute(
                        "UPDATE deliveries SET status = 'suppressed', lease_token = NULL, lease_until = NULL "
                        "WHERE event_id = ? AND user_id = ? AND id != ? AND status IN ('pending', 'leased')",
                        (row[0], row[1], delivery_id),
                    )
                return True
            return False

        return await self.writer.submit(_tx)

    async def complete_digest(self, deliveries: list[ClaimedDelivery], message_id: str) -> bool:
        """Acknowledge all digest members together, fenced by each delivery lease."""
        if not deliveries or not message_id or len({d.id for d in deliveries}) != len(deliveries):
            return False
        first = deliveries[0]
        scope = (first.user_id, first.channel, first.destination_version)
        if first.channel != "telegram" or any(
            (d.user_id, d.channel, d.destination_version) != scope for d in deliveries
        ):
            return False

        async def _tx(c: aiosqlite.Connection) -> bool:
            for d in deliveries:
                row = await (await c.execute(
                    "SELECT 1 FROM deliveries WHERE id = ? AND event_id = ? AND user_id = ? "
                    "AND channel = ? AND destination_version = ? AND status = 'leased' "
                    "AND lease_token = ?",
                    (d.id, d.event_id, d.user_id, d.channel, d.destination_version, d.lease_token),
                )).fetchone()
                if row is None:
                    return False
            for d in deliveries:
                cur = await c.execute(
                    "UPDATE deliveries SET status = 'sent', message_id = ?, "
                    "lease_token = NULL, lease_until = NULL "
                    "WHERE id = ? AND status = 'leased' AND lease_token = ?",
                    (message_id, d.id, d.lease_token),
                )
                if cur.rowcount != 1:
                    raise RuntimeError("Digest acknowledgement lost its lease")
                await c.execute(
                    "UPDATE deliveries SET status = 'suppressed', lease_token = NULL, lease_until = NULL "
                    "WHERE event_id = ? AND user_id = ? AND id != ? AND status IN ('pending', 'leased')",
                    (d.event_id, d.user_id, d.id),
                )
            return True

        return await self.writer.submit(_tx)

    async def retry(
        self, delivery_id: str, lease_token: str, retry_after_seconds: int = 30
    ) -> bool:
        avail_at = (datetime.now(UTC) + timedelta(seconds=retry_after_seconds)).isoformat()

        async def _tx(c: aiosqlite.Connection) -> bool:
            cur = await c.execute(
                "UPDATE deliveries SET status = 'pending', available_at = ?, "
                "lease_token = NULL, lease_until = NULL "
                "WHERE id = ? AND status = 'leased' AND lease_token = ?",
                (avail_at, delivery_id, lease_token),
            )
            return cur.rowcount > 0

        return await self.writer.submit(_tx)

    async def retry_with_vk_fallback(
        self, delivery: ClaimedDelivery, retry_after_seconds: int = 30
    ) -> bool:
        """Atomically retry a leased Telegram intent and enqueue its verified VK fallback."""
        now = datetime.now(UTC)
        available = (now + timedelta(seconds=retry_after_seconds)).isoformat()

        async def _tx(c: aiosqlite.Connection) -> bool:
            row = await (await c.execute(
                "SELECT event_id, user_id, payload_version, payload FROM deliveries "
                "WHERE id = ? AND channel = 'telegram' AND status = 'leased' AND lease_token = ?",
                (delivery.id, delivery.lease_token),
            )).fetchone()
            if row is None:
                return False
            await c.execute(
                "UPDATE deliveries SET status = 'pending', available_at = ?, "
                "lease_token = NULL, lease_until = NULL WHERE id = ?",
                (available, delivery.id),
            )
            destination = await (await c.execute(
                "SELECT address, version FROM channel_destinations "
                "WHERE user_id = ? AND channel = 'vk' AND verified = 1", (row[1],)
            )).fetchone()
            address = str(destination[0]) if destination else ""
            if address.isascii() and address.isdigit() and 0 < int(address) < 2_000_000_000:
                await c.execute(
                    "INSERT OR IGNORE INTO deliveries (id, event_id, user_id, channel, "
                    "destination_version, payload_version, payload, status, available_at) "
                    "VALUES (?, ?, ?, 'vk', ?, ?, ?, 'pending', ?)",
                    (f"del_{row[0]}_{row[1]}_vk", row[0], row[1], destination[1],
                     row[2], row[3], now.isoformat()),
                )
            return True

        return await self.writer.submit(_tx)

    async def get_cooldown_until(self, key: str) -> str | None:
        return (await self.get_cooldowns((key,))).get(key)

    async def get_cooldowns(self, keys: tuple[str, ...]) -> dict[str, str]:
        if not keys:
            return {}
        reader = await self.mgr.open_read_connection()
        try:
            placeholders = ",".join("?" for _ in keys)
            rows = await (await reader.execute(
                f"SELECT key, value FROM runtime WHERE key IN ({placeholders})", keys
            )).fetchall()
            return {str(key): str(value) for key, value in rows if value}
        finally:
            await self.mgr.close_reader(reader)

    async def get_telegram_chat_keys(self, user_ids: tuple[int, ...]) -> dict[int, str]:
        keys = {uid: f"user:{uid}" for uid in user_ids}
        if not user_ids:
            return keys
        reader = await self.mgr.open_read_connection()
        try:
            placeholders = ",".join("?" for _ in user_ids)
            rows = await (await reader.execute(
                "SELECT user_id, address FROM channel_destinations "
                f"WHERE channel='telegram' AND verified=1 AND user_id IN ({placeholders})",
                user_ids,
            )).fetchall()
            for user_id, address in rows:
                digest = hashlib.sha256(str(address).encode("utf-8")).hexdigest()[:20]
                keys[int(user_id)] = f"chat:{digest}"
            return keys
        finally:
            await self.mgr.close_reader(reader)

    async def extend_cooldown(self, key: str, until: datetime) -> None:
        deadline = until.astimezone(UTC).isoformat()

        async def _tx(c: aiosqlite.Connection) -> None:
            row = await (await c.execute(
                "SELECT value FROM runtime WHERE key = ?", (key,)
            )).fetchone()
            if row and str(row[0]) >= deadline:
                return
            await c.execute(
                "INSERT INTO runtime(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, deadline),
            )

        await self.writer.submit(_tx)

    async def dead_letter(self, delivery_id: str, lease_token: str) -> bool:
        async def _tx(c: aiosqlite.Connection) -> bool:
            cur = await c.execute(
                "UPDATE deliveries SET status = 'dead_letter', lease_token = NULL, "
                "lease_until = NULL "
                "WHERE id = ? AND status = 'leased' AND lease_token = ?",
                (delivery_id, lease_token),
            )
            return cur.rowcount > 0

        return await self.writer.submit(_tx)

    async def expire(self, delivery_id: str, lease_token: str) -> bool:
        async def _tx(c: aiosqlite.Connection) -> bool:
            cur = await c.execute(
                "UPDATE deliveries SET status = 'expired', lease_token = NULL, lease_until = NULL "
                "WHERE id = ? AND status = 'leased' AND lease_token = ?",
                (delivery_id, lease_token),
            )
            return cur.rowcount > 0

        return await self.writer.submit(_tx)

    async def count_active_for_user(self, user_id: int) -> int:
        async def _tx(c: aiosqlite.Connection) -> int:
            sql = (
                "SELECT COUNT(*) FROM deliveries WHERE user_id = ? "
                "AND status IN ('pending', 'leased')"
            )
            row = await (await c.execute(sql, (int(user_id),))).fetchone()
            return int(row[0]) if row else 0

        return await self.writer.submit(_tx)
