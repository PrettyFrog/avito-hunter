"""End-to-end integration test of the local simulated stand (<= 400 lines)."""

from __future__ import annotations

import asyncio
import pytest

from tools.stand import create_stand


@pytest.mark.asyncio
async def test_stand_owner_and_stranger_flow() -> None:
    stand = await create_stand(owner_chat_id=111111, stranger_chat_id=999999)
    try:
        # 1. Owner interacts with /start
        await stand.owner.send_command("start")
        owner_msg = await stand.owner.wait_for_message(timeout=5.0)
        assert "панель владельца" in owner_msg.text.lower() or "привет" in owner_msg.text.lower()

        # 2. Stranger sends /start and gets access request / closed bot notice
        await stand.stranger.send_command("start")
        stranger_msg = await stand.stranger.wait_for_message(timeout=5.0)
        assert "закрытый бот" in stranger_msg.text.lower()

        # 3. Fake Avito item can be added and queried
        item = stand.avito.add_item(id="12345", title="Наушники Sony WH-1000XM5", price=18000)
        assert item.id == "12345"
        assert item.price == 18000
    finally:
        await stand.stop()
