from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hunter.application.digest_dispatch import try_send_digest
from hunter.application.dispatcher import SendResult
from hunter.storage.deliveries import ClaimedDelivery


def item(number, attempt=1):
    return ClaimedDelivery(
        f"d{number}",
        f"e{number}",
        1,
        "telegram",
        1,
        {
            "title": "Item",
            "new_price": 100,
            "url": f"https://avito.ru/{number}",
            "quiet_digest": True,
        },
        attempt,
        "lease",
        "",
        1,
        datetime.now(UTC).isoformat(),
    )


def dispatcher(result):
    sender = SimpleNamespace(send_digest=AsyncMock(return_value=result))
    repo = SimpleNamespace(
        is_user_active=AsyncMock(return_value=True),
        complete_digest=AsyncMock(return_value=True),
        retry=AsyncMock(return_value=True),
        dead_letter=AsyncMock(return_value=True),
        retry_with_vk_fallback=AsyncMock(return_value=True),
    )
    return SimpleNamespace(
        senders={"telegram": sender},
        repo=repo,
        enforce_user_status=True,
        cutover_manager=None,
        _telegram_cooldown_seconds=AsyncMock(return_value=0),
        _producer_is_current=AsyncMock(return_value=True),
        _pace_telegram_send=AsyncMock(),
        _persist_retry_cooldown=AsyncMock(),
        _parse_delivery_time=lambda value: datetime.fromisoformat(value) if value else None,
    )


@pytest.mark.asyncio
async def test_digest_batch_sends_once_and_acknowledges_all():
    d = dispatcher(SendResult("sent", message_id="42"))
    items = [item(1), item(2)]
    consumed, result = await try_send_digest(d, items, datetime.now(UTC))
    assert consumed == 2 and result.status == "sent"
    d.senders["telegram"].send_digest.assert_awaited_once()
    d.repo.complete_digest.assert_awaited_once_with(items, "42")


@pytest.mark.asyncio
async def test_digest_timeout_retries_every_item_and_uses_vk_fallback():
    d = dispatcher(SendResult("ambiguous", retry_after_seconds=7))
    d.senders["vk"] = object()
    items = [item(1, 3), item(2, 1)]
    consumed, result = await try_send_digest(d, items, datetime.now(UTC))
    assert consumed == 2 and result.status == "ambiguous"
    d.repo.retry_with_vk_fallback.assert_awaited_once_with(items[0], 7)
    d.repo.retry.assert_awaited_once_with("d2", "lease", retry_after_seconds=7)
    d.repo.complete_digest.assert_not_awaited()


@pytest.mark.asyncio
async def test_digest_ack_storage_fault_does_not_confirm_items():
    d = dispatcher(SendResult("sent", message_id="42"))
    d.repo.complete_digest.side_effect = OSError("injected failure")
    with pytest.raises(OSError, match="injected failure"):
        await try_send_digest(d, [item(1), item(2)], datetime.now(UTC))
    d.repo.retry.assert_not_awaited()


@pytest.mark.asyncio
async def test_digest_preflight_rejects_inactive_and_superseded_tenant():
    d = dispatcher(SendResult("sent", message_id="42"))
    d.repo.is_user_active.return_value = False
    assert await try_send_digest(d, [item(1), item(2)], datetime.now(UTC)) is None
    d.repo.is_user_active.return_value = True
    d._producer_is_current.return_value = False
    assert await try_send_digest(d, [item(1), item(2)], datetime.now(UTC)) is None
    d.senders["telegram"].send_digest.assert_not_awaited()
