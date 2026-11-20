"""Send one eligible digest chunk without weakening outbox fencing."""

from datetime import datetime
from typing import Any

from hunter.application.dispatcher import SendResult
from hunter.domain.delivery_policy import is_delivery_expired
from hunter.notify.digest_delivery import format_digest_chunks
from hunter.storage.deliveries import ClaimedDelivery


async def _preflight(dispatcher: Any, batch: list[ClaimedDelivery], now: datetime) -> bool:
    first = batch[0]
    if dispatcher.enforce_user_status and not await dispatcher.repo.is_user_active(first.user_id):
        return False
    if await dispatcher._telegram_cooldown_seconds(first.user_id):
        return False
    for delivery in batch:
        if getattr(dispatcher, "revalidate_deliveries", False):
            from hunter.application.delivery_eligibility import (
                DeliveryEligibility,
                check_delivery_eligibility,
            )
            eligibility = await check_delivery_eligibility(dispatcher.repo.mgr, delivery)
            if eligibility != DeliveryEligibility.ELIGIBLE:
                return False
        created = dispatcher._parse_delivery_time(delivery.created_at)
        if delivery.created_at and created is None:
            return False
        if created and is_delivery_expired(created, now):
            return False
        if not await dispatcher._producer_is_current(delivery):
            return False
    return True


async def _retry_batch(
    dispatcher: Any, batch: list[ClaimedDelivery], retry_after_seconds: int = 30
) -> None:
    for delivery in batch:
        if delivery.attempt >= 3 and "vk" in dispatcher.senders:
            await dispatcher.repo.retry_with_vk_fallback(delivery, retry_after_seconds)
        else:
            await dispatcher.repo.retry(
                delivery.id,
                delivery.lease_token,
                retry_after_seconds=retry_after_seconds,
            )


async def _finish(dispatcher: Any, batch: list[ClaimedDelivery], result: SendResult) -> SendResult:
    await dispatcher._persist_retry_cooldown(batch[0], result)
    if result.status == "sent":
        if not await dispatcher.repo.complete_digest(batch, result.message_id):
            await _retry_batch(dispatcher, batch, result.retry_after_seconds)
            return SendResult(
                "retry",
                error_message="Digest acknowledgement lost its lease",
                retry_after_seconds=result.retry_after_seconds,
            )
    elif result.status in ("retry", "ambiguous"):
        await _retry_batch(dispatcher, batch, result.retry_after_seconds)
    elif result.status == "dead_letter":
        for delivery in batch:
            await dispatcher.repo.dead_letter(delivery.id, delivery.lease_token)
    return result


async def try_send_digest(
    dispatcher: Any, deliveries: list[ClaimedDelivery], now: datetime
) -> tuple[int, SendResult] | None:
    if len(deliveries) < 2 or any(d.payload.get("quiet_digest") is not True for d in deliveries):
        return None
    sender = dispatcher.senders.get("telegram")
    send = getattr(sender, "send_digest", None)
    if not callable(send):
        return None
    try:
        chunks = format_digest_chunks(deliveries)
    except (TypeError, ValueError):
        return None
    chunk = chunks[0]
    batch = deliveries[: len(chunk.delivery_ids)]
    if len(batch) < 2 or not await _preflight(dispatcher, batch, now):
        return None
    await dispatcher._pace_telegram_send(batch[0].user_id)
    if not await _preflight(dispatcher, batch, now):
        return None
    try:
        result = await send(batch, chunk)
    except TimeoutError:
        result = SendResult("ambiguous", error_message="Digest send timed out")
    except Exception:
        result = SendResult("retry", error_message="Digest send failed")
    return len(batch), await _finish(dispatcher, batch, result)
