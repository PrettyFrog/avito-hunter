"""Public wizard must never silently transform a malformed price."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hunter.notify.telegram_handlers.public_add import _handle_price_step


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text", ["-50000", "10.50", "10,50", "abc100", "10-20", "0", "9223372036854775808"]
)
async def test_invalid_price_keeps_dialog_unchanged(text):
    message = SimpleNamespace(reply=AsyncMock())
    db = SimpleNamespace(save_pending_product=AsyncMock())
    user = SimpleNamespace(id=1)
    await _handle_price_step(
        message, db, user, 101, {"url": "https://avito.ru/moskva?q=house"}, text
    )
    db.save_pending_product.assert_not_awaited()
    assert "Неверный формат цены" in message.reply.await_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("text,expected", [("10 000 000 000 ₽", 10_000_000_000), ("50000", 50000)])
async def test_valid_price_preserved(text, expected):
    message = SimpleNamespace(reply=AsyncMock())
    db = SimpleNamespace(save_pending_product=AsyncMock())
    user = SimpleNamespace(id=1)
    payload = {"url": "https://avito.ru/moskva?q=house"}
    await _handle_price_step(message, db, user, 101, payload, text)
    assert payload["price"] == expected
    db.save_pending_product.assert_awaited_once()
