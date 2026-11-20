"""Typed VK delivery adapter with verified destinations and durable fallback (S6 / T-487)."""

from __future__ import annotations

import asyncio
import json
import logging
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import aiosqlite
import httpx

from hunter.application.dispatcher import SendResult
from hunter.core.models import NotificationEvent, now_utc
from hunter.notify.telegram_delivery import to_short_avito_url
from hunter.storage.deliveries import ClaimedDelivery

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class VkDeliveryEnvelope:
    destination: int
    text: str
    delivery_id: str
    event_id: str = ""
    user_id: int = 0
    random_id: int | None = None


@dataclass(frozen=True, slots=True)
class VkDeliveryResult:
    status: Literal["sent", "retry", "blocked", "permanent", "ambiguous"]
    message_id: str = ""
    retry_after: int = 30
    error_message: str = ""

    def to_send_result(self) -> SendResult:
        if self.status == "sent":
            return SendResult(status="sent", message_id=self.message_id)
        if self.status in ("blocked", "permanent"):
            return SendResult(
                status="dead_letter", error_message=self.error_message,
                recipient_blocked=self.status == "blocked",
            )
        return SendResult(
            status="ambiguous" if self.status == "ambiguous" else "retry",
            retry_after_seconds=self.retry_after,
            error_message=self.error_message,
        )


def get_stable_random_id(did: str) -> int:
    return zlib.crc32(did.encode("utf-8")) & 0x7FFFFFFF


def format_vk_delivery_text(p: dict[str, Any]) -> str:
    title, price, old_p, url, aid = (
        p.get("title", ""),
        p.get("new_price", p.get("price", 0)),
        p.get("old_price"),
        p.get("url", ""),
        p.get("avito_id"),
    )
    clean_url = to_short_avito_url(url, avito_id=aid)
    h = (
        f"💱 {title}\nЦена изменилась: {old_p} ₽ ➔ {price} ₽"
        if old_p is not None and old_p != price
        else f"🎧 {title}\nЦена: {price} ₽"
    )
    return f"{h}\n{clean_url}"


async def get_verified_destination(conn: aiosqlite.Connection, user_id: int) -> int | None:
    row = await (
        await conn.execute(
            "SELECT address FROM channel_destinations WHERE user_id = ? "
            "AND channel = 'vk' AND verified = 1",
            (user_id,),
        )
    ).fetchone()
    return int(row[0]) if row and str(row[0]).isdigit() else None


async def resolve_recipient(db, event: NotificationEvent, default_uid: int | None) -> int | None:
    uid = getattr(event, "recipient_user_id", None)
    if not uid or not db:
        return default_uid
    try:
        c = await db.connect() if hasattr(db, "connect") else db
        return await get_verified_destination(c, uid)
    except Exception:
        return None


async def handle_vk_error(err: dict, user_id: int | None, db=None, telegram_notifier=None) -> None:
    code, msg = err.get("error_code", 0), err.get("error_msg", "VK error")
    logger.warning("VK API messages.send error %s: %s", code, msg)
    if db and telegram_notifier:
        try:
            last = await db.get_runtime("last_alert:vk_error")
            if not last or (now_utc() - datetime.fromisoformat(last)).total_seconds() >= 21600:
                await db.set_runtime("last_alert:vk_error", now_utc().isoformat())
                await telegram_notifier.alert(f"⚠️ Ошибка VK API ({code}): {msg}.")
        except Exception:
            pass


async def create_vk_fallback_delivery(
    conn: aiosqlite.Connection, d: ClaimedDelivery, delay_seconds: int = 600
) -> str | None:
    if not (await get_verified_destination(conn, d.user_id)):
        return None
    vk_id, now = f"del_{d.event_id}_{d.user_id}_vk", datetime.now(UTC)
    avail = (now + timedelta(seconds=delay_seconds)).isoformat()
    sql = (
        "INSERT OR IGNORE INTO deliveries (id, event_id, user_id, channel, "
        "destination_version, payload_version, payload, status, available_at, "
        "attempt, created_at) VALUES (?, ?, ?, 'vk', "
        "(SELECT version FROM channel_destinations WHERE user_id = ? "
        "AND channel = 'vk' AND verified = 1), 1, ?, 'pending', ?, 0, ?)"
    )
    await conn.execute(
        sql,
        (vk_id, d.event_id, d.user_id, d.user_id, json.dumps(d.payload), avail, now.isoformat()),
    )
    return vk_id


class VkDeliveryAdapter:
    def __init__(
        self, token: str, version: str = "5.199", client: httpx.AsyncClient | None = None
    ) -> None:
        self.token, self.version, self.client = token, version, client

    async def send_envelope(self, env: VkDeliveryEnvelope) -> VkDeliveryResult:
        rid = env.random_id or get_stable_random_id(env.delivery_id)
        data = {
            "access_token": self.token,
            "user_id": env.destination,
            "random_id": rid,
            "message": env.text,
            "v": self.version,
        }
        c = self.client or httpx.AsyncClient(timeout=10.0)
        try:
            response = await c.post("https://api.vk.com/method/messages.send", data=data)
            response.raise_for_status()
            res = response.json()
            if "error" in res:
                code, msg = (
                    res["error"].get("error_code", 0),
                    res["error"].get("error_msg", "VK error"),
                )
                st = "blocked" if code == 901 else ("permanent" if code == 5 else "retry")
                return VkDeliveryResult(
                    status=st, retry_after=60 if code == 9 else 30, error_message=msg
                )
            message_id = res.get("response")
            if type(message_id) is not int or message_id <= 0:
                return VkDeliveryResult(
                    status="ambiguous", error_message="VK response lacks a valid message ID"
                )
            return VkDeliveryResult(status="sent", message_id=str(message_id))
        except httpx.HTTPStatusError as exc:
            status = "retry" if exc.response.status_code == 429 else "ambiguous"
            return VkDeliveryResult(status=status, error_message="VK returned an HTTP error")
        except (httpx.TransportError, TimeoutError, asyncio.TimeoutError) as exc:
            return VkDeliveryResult(status="ambiguous", retry_after=30, error_message=str(exc))
        except Exception as exc:
            return VkDeliveryResult(status="retry", retry_after=30, error_message=str(exc))
        finally:
            if self.client is None:
                await c.aclose()

    async def send_delivery(self, delivery: ClaimedDelivery, destination: int) -> SendResult:
        res = await self.send_envelope(
            VkDeliveryEnvelope(
                destination=destination,
                text=format_vk_delivery_text(delivery.payload),
                delivery_id=delivery.id,
                event_id=delivery.event_id,
                user_id=delivery.user_id,
            )
        )
        return res.to_send_result()
