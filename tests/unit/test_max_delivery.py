"""MAX remains an explicit no-send state until its contract is verified."""

from __future__ import annotations

import pytest

from hunter.bootstrap import Bootstrap
from hunter.core.config import Settings
from hunter.notify.max_delivery import (
    MAX_RECHECK_SECONDS,
    MAX_UNSUPPORTED_REASON,
    MaxDeliveryAdapter,
)
from hunter.storage.deliveries import ClaimedDelivery


def _delivery(payload: dict) -> ClaimedDelivery:
    return ClaimedDelivery(
        id="delivery-max-1",
        event_id="event-max-1",
        user_id=52,
        channel="max",
        payload_version=1,
        payload=payload,
        attempt=1,
        lease_token="lease-test",
        lease_until="2030-01-01T00:00:30+00:00",
    )


@pytest.mark.asyncio
async def test_unsupported_max_is_retryable_and_never_acknowledged() -> None:
    result = await MaxDeliveryAdapter()(_delivery({"text": "test"}))
    assert result.status == "retry"
    assert result.retry_after_seconds == MAX_RECHECK_SECONDS
    assert result.error_message == MAX_UNSUPPORTED_REASON
    assert result.message_id == ""


@pytest.mark.asyncio
async def test_forged_max_binding_payload_stays_unsupported() -> None:
    forged = {"destination": "attacker-id", "verified": True, "binding_token": "fake"}
    result = await MaxDeliveryAdapter()(_delivery(forged))
    assert result.status == "retry"
    assert "unsupported" in result.error_message
    assert "attacker-id" not in result.error_message


def test_bootstrap_registers_only_fail_closed_max_sender() -> None:
    telegram = object()
    bootstrap = Bootstrap(Settings(_env_file=None), senders={"telegram": telegram})
    assert bootstrap.senders["telegram"] is telegram
    assert isinstance(bootstrap.senders["max"], MaxDeliveryAdapter)
    assert "vk" not in bootstrap.senders
