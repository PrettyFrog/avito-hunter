"""Fail-closed MAX delivery port until its API and verified binding are available."""

from __future__ import annotations

from hunter.application.dispatcher import SendResult
from hunter.storage.deliveries import ClaimedDelivery

MAX_UNSUPPORTED_REASON = (
    "MAX delivery is unsupported: no verified API contract or destination binding is configured"
)
MAX_RECHECK_SECONDS = 24 * 60 * 60


class MaxDeliveryAdapter:
    """Keep MAX intents retryable without claiming delivery or making network calls."""

    async def __call__(self, delivery: ClaimedDelivery) -> SendResult:
        if delivery.channel != "max":
            return SendResult(
                "dead_letter", error_message="MAX adapter received a different channel"
            )
        return SendResult(
            "retry",
            retry_after_seconds=MAX_RECHECK_SECONDS,
            error_message=MAX_UNSUPPORTED_REASON,
        )
