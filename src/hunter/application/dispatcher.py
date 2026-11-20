"Independent delivery dispatcher with wake-on-commit, per-recipient pacing, and fairness."

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from hunter.domain.delivery_policy import is_delivery_expired
from hunter.storage.deliveries import ClaimedDelivery, DeliveryRepository

logger = logging.getLogger(__name__)
TELEGRAM_GLOBAL_INTERVAL_SECONDS = 0.041  # # CALIBRATE: 25/s plus scheduling jitter margin.
TELEGRAM_CHAT_INTERVAL_SECONDS = 0.50
TELEGRAM_GLOBAL_COOLDOWN_KEY = "delivery:telegram:global"


@dataclass(frozen=True, slots=True)
class SendResult:
    status: Literal["sent", "retry", "dead_letter", "ambiguous", "expired", "suppressed"]
    message_id: str = ""
    retry_after_seconds: int = 30
    error_message: str = ""
    retry_scope: Literal["delivery", "chat", "global"] = "delivery"
    recipient_blocked: bool = False


SenderFn = Callable[[ClaimedDelivery], Awaitable[SendResult]]


class DeliveryDispatcher:
    """Dispatches claimed deliveries outside SQLite transactions with fair pacing."""

    def __init__(
        self,
        repo: DeliveryRepository,
        senders: dict[str, SenderFn],
        batch_size: int = 10,
        cutover_manager: Any = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        enforce_user_status: bool = False,
        revalidate_deliveries: bool = False,
    ) -> None:
        self.repo = repo
        self.senders = senders
        self.batch_size = batch_size
        self.cutover_manager = cutover_manager
        self.enforce_user_status = enforce_user_status
        self.revalidate_deliveries = revalidate_deliveries
        self._clock = clock
        self._sleep = sleep
        # Leave one quiet second after restart so the in-memory bucket cannot reset into a burst.
        self._global_next_send = self._clock() + 1.0
        self._chat_next_send: dict[str, float] = {}
        self._chat_scope_by_user: dict[int, str] = {}
        self._cooldowns: dict[str, str] = {}
        self._rate_lock = asyncio.Lock()
        self._wake_event = asyncio.Event()
        self._running = False
        self._task: asyncio.Task | None = None

    def wake_up(self) -> None:
        self._wake_event.set()

    @staticmethod
    def _chat_cooldown_key(chat_scope: str) -> str:
        return f"delivery:telegram:{chat_scope}"

    @staticmethod
    def _remaining_cooldown(until_value: str | None, now: datetime) -> int:
        if not until_value:
            return 0
        try:
            until = DeliveryDispatcher._parse_delivery_time(until_value)
        except (TypeError, ValueError):
            return 0
        return max(0, math.ceil((until - now).total_seconds())) if until else 0

    async def _telegram_cooldown_seconds(self, user_id: int) -> int:
        now = datetime.now(UTC)
        chat_scope = self._chat_scope_by_user.get(user_id, f"user:{user_id}")
        global_cd = self._remaining_cooldown(self._cooldowns.get(TELEGRAM_GLOBAL_COOLDOWN_KEY), now)
        chat_cd = self._remaining_cooldown(self._cooldowns.get(self._chat_cooldown_key(chat_scope)), now)
        if chat_cd > 300:
            chat_cd = 0
        return max(global_cd, chat_cd)

    async def _pace_telegram_send(self, user_id: int) -> None:
        chat_scope = self._chat_scope_by_user.get(user_id, f"user:{user_id}")
        while True:
            reserved = False
            async with self._rate_lock:
                now = self._clock()
                chat_ready = self._chat_next_send.get(chat_scope, now)
                if chat_ready > now:
                    delay = chat_ready - now
                else:
                    slot = max(now, self._global_next_send)
                    self._global_next_send = slot + TELEGRAM_GLOBAL_INTERVAL_SECONDS
                    self._chat_next_send[chat_scope] = slot + TELEGRAM_CHAT_INTERVAL_SECONDS
                    delay = slot - now
                    reserved = True
            if delay > 0:
                await self._sleep(delay)
            if reserved:
                return

    async def _persist_retry_cooldown(self, delivery: ClaimedDelivery, result: SendResult) -> None:
        if (
            result.status != "retry"
            or delivery.channel != "telegram"
            or result.retry_scope == "delivery"
        ):
            return
        key = (
            TELEGRAM_GLOBAL_COOLDOWN_KEY
            if result.retry_scope == "global"
            else self._chat_cooldown_key(
                self._chat_scope_by_user.get(delivery.user_id, f"user:{delivery.user_id}")
            )
        )
        until = datetime.now(UTC).timestamp() + max(1, result.retry_after_seconds)
        deadline = datetime.fromtimestamp(until, UTC)
        existing = self._cooldowns.get(key)
        if self._remaining_cooldown(existing, datetime.now(UTC)) < result.retry_after_seconds:
            self._cooldowns[key] = deadline.isoformat()
        await self.repo.extend_cooldown(key, deadline)

    async def _handle_delivery(self, delivery: ClaimedDelivery, now_dt: datetime) -> SendResult:
        age_result = await self._check_delivery_age(delivery, now_dt)
        if age_result is not None:
            return age_result
        if self.enforce_user_status and not await self.repo.is_user_active(delivery.user_id):
            return await self._handle_inactive_user(delivery)

        sender = self.senders.get(delivery.channel)
        if not sender:
            return await self._handle_missing_sender(delivery)
        if delivery.channel == "telegram":
            cooldown = await self._telegram_cooldown_seconds(delivery.user_id)
            if cooldown:
                await self.repo.retry(delivery.id, delivery.lease_token, cooldown)
                return SendResult(
                    "retry", retry_after_seconds=cooldown, error_message="Telegram rate cooldown"
                )
            await self._pace_telegram_send(delivery.user_id)
        if self.revalidate_deliveries:
            gated = await self._check_current_delivery(delivery)
            if gated is not None:
                return gated
        try:
            res = await sender(delivery)
        except TimeoutError:
            res = SendResult(
                status="ambiguous", retry_after_seconds=30, error_message="Send timed out"
            )
        except Exception as exc:
            res = SendResult(status="retry", retry_after_seconds=30, error_message=str(exc))

        await self._persist_retry_cooldown(delivery, res)
        if res.status == "sent":
            await self.repo.complete(delivery.id, delivery.lease_token, message_id=res.message_id)
        elif res.status in ("retry", "ambiguous"):
            if delivery.channel == "telegram" and delivery.attempt >= 3 and "vk" in self.senders:
                await self.repo.retry_with_vk_fallback(delivery, res.retry_after_seconds)
            else:
                await self.repo.retry(
                    delivery.id, delivery.lease_token, retry_after_seconds=res.retry_after_seconds
                )
        elif res.status == "dead_letter":
            await self.repo.dead_letter(delivery.id, delivery.lease_token)
        return res

    async def _check_delivery_age(
        self, delivery: ClaimedDelivery, now_dt: datetime
    ) -> SendResult | None:
        created_at = self._parse_delivery_time(delivery.created_at)
        if delivery.created_at and created_at is None:
            await self.repo.dead_letter(delivery.id, delivery.lease_token)
            return SendResult(status="dead_letter", error_message="Invalid delivery timestamp")
        if created_at and is_delivery_expired(created_at, now_dt):
            await self.repo.expire(delivery.id, delivery.lease_token)
            return SendResult(status="expired", error_message="Delivery exceeded retention")
        return None

    async def _check_current_delivery(self, delivery: ClaimedDelivery) -> SendResult | None:
        from hunter.application.delivery_eligibility import (
            DeliveryEligibility,
            check_delivery_eligibility,
            suppress_if_still_ineligible,
        )

        result = await check_delivery_eligibility(self.repo.mgr, delivery)
        if result == DeliveryEligibility.ELIGIBLE:
            return None
        if result == DeliveryEligibility.SUPPRESS:
            if await suppress_if_still_ineligible(self.repo, delivery):
                return SendResult("suppressed", error_message="Subscription filters changed")
        await self.repo.retry(delivery.id, delivery.lease_token, retry_after_seconds=30)
        return SendResult("retry", error_message="Delivery eligibility not confirmed")

    async def _handle_missing_sender(self, delivery: ClaimedDelivery) -> SendResult:
        if delivery.channel == "vk":
            await self.repo.retry(delivery.id, delivery.lease_token, retry_after_seconds=300)
            return SendResult(
                "retry", retry_after_seconds=300, error_message="VK sender unavailable"
            )
        await self.repo.dead_letter(delivery.id, delivery.lease_token)
        return SendResult(status="dead_letter", error_message=f"No sender for {delivery.channel}")

    async def _handle_inactive_user(self, delivery: ClaimedDelivery) -> SendResult:
        status = await self.repo.get_user_status(delivery.user_id) if hasattr(self.repo, "get_user_status") else None
        if status in ("deleted", "blocked") or status is None:
            await self.repo.dead_letter(delivery.id, delivery.lease_token)
            return SendResult(status="dead_letter", error_message=f"User status is {status}")
        await self.repo.retry(delivery.id, delivery.lease_token, retry_after_seconds=300)
        return SendResult("retry", retry_after_seconds=300, error_message=f"User is {status}")

    @staticmethod
    def _parse_delivery_time(value: str) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    async def _producer_is_current(self, delivery: ClaimedDelivery) -> bool:
        return True

    async def _dispatch_recipient_queue(
        self, deliveries: list[ClaimedDelivery], now_dt: datetime
    ) -> None:
        from hunter.application.digest_dispatch import try_send_digest

        i = 0
        while i < len(deliveries):
            d = deliveries[i]
            end = i + 1
            if d.channel == "telegram" and d.payload.get("quiet_digest") is True:
                while end < len(deliveries):
                    nxt = deliveries[end]
                    if (nxt.payload.get("quiet_digest") is not True
                            or nxt.destination_version != d.destination_version):
                        break
                    end += 1
            digest = await try_send_digest(self, deliveries[i:end], now_dt) if end > i + 1 else None
            if digest is None:
                res = await self._handle_delivery(d, now_dt)
                i += 1
            else:
                consumed, res = digest
                i += consumed
            if res.status in ("retry", "ambiguous"):
                backoff = res.retry_after_seconds
                for rem in deliveries[i:]:
                    await self.repo.retry(rem.id, rem.lease_token, retry_after_seconds=backoff)
                break

    async def dispatch_once(self, now_ts: str | None = None) -> int:
        ts = now_ts or datetime.now(UTC).isoformat()
        now_dt = self._parse_delivery_time(ts) or datetime.now(UTC)
        global_cooldown = await self.repo.get_cooldown_until(TELEGRAM_GLOBAL_COOLDOWN_KEY)
        if self._remaining_cooldown(global_cooldown, datetime.now(UTC)):
            claimed = await self.repo.claim_due(ts, limit=self.batch_size, exclude_telegram=True)
        else:
            claimed = await self.repo.claim_due(ts, limit=self.batch_size)
        if not claimed:
            return 0
        keys = {TELEGRAM_GLOBAL_COOLDOWN_KEY}
        self._chat_scope_by_user = await self.repo.get_telegram_chat_keys(
            tuple({item.user_id for item in claimed if item.channel == "telegram"})
        )
        keys.update(
            self._chat_cooldown_key(self._chat_scope_by_user[item.user_id])
            for item in claimed
            if item.channel == "telegram"
        )
        self._cooldowns = await self.repo.get_cooldowns(tuple(keys))
        by_recipient: dict[tuple[str, int], list[ClaimedDelivery]] = {}
        for d in claimed:
            by_recipient.setdefault((d.channel, d.user_id), []).append(d)
        await asyncio.gather(
            *(self._dispatch_recipient_queue(q, now_dt) for q in by_recipient.values())
        )
        return len(claimed)

    async def run_loop(self, poll_interval: float = 1.0) -> None:
        self._running = True
        if self.repo and hasattr(self.repo, "purge_deleted_user_deliveries"):
            try:
                await self.repo.purge_deleted_user_deliveries()
            except Exception as exc:
                logger.warning("Error purging deleted user deliveries: %s", exc)
        while self._running:
            try:
                if await self.dispatch_once() == 0:
                    try:
                        await asyncio.wait_for(self._wake_event.wait(), timeout=poll_interval)
                    except TimeoutError:
                        pass
                    finally:
                        self._wake_event.clear()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("Error in dispatcher loop: %s", exc)
                await asyncio.sleep(poll_interval)

    def start(self, poll_interval: float = 1.0) -> None:
        if not self._task or self._task.done():
            self._task = asyncio.create_task(self.run_loop(poll_interval))

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
        for sender in self.senders.values():
            if hasattr(sender, "aclose"):
                try:
                    await sender.aclose()
                except Exception as exc:
                    logger.warning("Error closing sender: %s", exc)
