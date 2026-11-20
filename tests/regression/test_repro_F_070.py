"""Regression test for F-070: Failed complete_digest skips retry and causes duplicate loop."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
import pytest

from hunter.application.dispatcher import DeliveryDispatcher
from hunter.storage.deliveries import ClaimedDelivery, DeliveryRepository


@pytest.mark.asyncio
async def test_repro_F_070_digest_retry_not_skipped_on_lease_lost() -> None:
    repo = MagicMock(spec=DeliveryRepository)
    repo.mgr = MagicMock()
    repo.writer = MagicMock()
    repo.get_cooldown_until = AsyncMock(return_value=None)
    repo.get_cooldowns = AsyncMock(return_value={})
    repo.get_telegram_chat_keys = AsyncMock(return_value={1: "chat_1"})
    repo.retry = AsyncMock()

    p1 = {"quiet_digest": True, "avito_id": "111", "title": "Item 1", "url": "https://avito.ru/1", "price": 10000}
    p2 = {"quiet_digest": True, "avito_id": "222", "title": "Item 2", "url": "https://avito.ru/2", "price": 20000}
    p3 = {"quiet_digest": False, "avito_id": "333", "title": "Item 3", "url": "https://avito.ru/3", "price": 30000}

    d1 = ClaimedDelivery(
        id="del_1", event_id="ev_1", user_id=1, channel="telegram",
        payload_version=1, payload=p1,
        attempt=1, lease_token="tok_1", lease_until="2026-10-30T10:00:00Z"
    )
    d2 = ClaimedDelivery(
        id="del_2", event_id="ev_2", user_id=1, channel="telegram",
        payload_version=1, payload=p2,
        attempt=1, lease_token="tok_2", lease_until="2026-10-30T10:00:00Z"
    )
    d3 = ClaimedDelivery(
        id="del_3", event_id="ev_3", user_id=1, channel="telegram",
        payload_version=1, payload=p3,
        attempt=1, lease_token="tok_3", lease_until="2026-10-30T10:00:00Z"
    )

    repo.claim_due = AsyncMock(return_value=[d1, d2, d3])
    # complete_digest fails because lease expired
    repo.complete_digest = AsyncMock(return_value=False)

    sender = MagicMock()
    send_result = MagicMock()
    send_result.status = "sent"
    send_result.message_id = "msg_999"
    send_result.retry_after_seconds = 30
    sender.send_digest = AsyncMock(return_value=send_result)

    dispatcher = DeliveryDispatcher(repo=repo, senders={"telegram": sender})
    dispatcher._producer_is_current = AsyncMock(return_value=True)

    await dispatcher.dispatch_once("2026-10-30T10:00:00Z")

    retry_ids = [c.args[0] for c in repo.retry.call_args_list]
    # Desired behavior: batch deliveries MUST be retried or accounted for, not silently dropped from retry loop
    assert "del_1" in retry_ids and "del_2" in retry_ids, f"Batch deliveries were skipped from retry! Found: {retry_ids} (F-070)"
