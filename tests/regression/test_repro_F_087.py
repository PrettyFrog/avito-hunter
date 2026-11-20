"""Regression test for F-087: Approved stranger does not receive onboarding wizard (Scenario 1).

Verifies that:
1. Stranger requests access.
2. Owner approves access request.
3. Newly approved user immediately receives Onboarding Screen 1 with '[ Дальше ➡️ ]' button (onb:2).
4. Newly approved user can progress through Screen 2, Screen 3, and complete onboarding with main menu.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
import pytest
from aiogram.types import User as TgUser

from hunter.core.db import Database
from hunter.notify.messages_ru import (
    ONBOARDING_SCREEN_2,
)
from hunter.notify.telegram_router import setup_telegram_router
from tests.unit.test_multiuser_m2 import make_mock_callback, make_mock_message


@pytest.fixture
def mock_channel() -> MagicMock:
    ch = MagicMock()
    ch.chat_id = 1001  # Owner telegram ID
    ch.alert = AsyncMock(return_value=True)
    return ch


@pytest.fixture
def mock_bot() -> AsyncMock:
    bot = AsyncMock()
    bot.id = 123456
    bot.get_me = AsyncMock(
        return_value=TgUser(id=123456, is_bot=True, first_name="Bot", username="avito_bot")
    )
    bot.send_message = AsyncMock(return_value=MagicMock())
    return bot


@pytest.mark.asyncio
async def test_repro_f087_approved_stranger_receives_onboarding_wizard(
    tmp_path: Path, mock_channel: MagicMock, mock_bot: AsyncMock
) -> None:
    db = Database(tmp_path / "hunter.db")
    await db.connect()

    # 1. Setup owner and router
    await db.create_user(telegram_user_id=1001, chat_id=1001, role="owner", status="active")
    router = setup_telegram_router(mock_channel, db=db)

    # 2. Stranger requests access
    stranger_tg = TgUser(id=6001, is_bot=False, first_name="Иван", username="ivan_guest")
    cb_msg = make_mock_message(chat_id=6001, user=stranger_tg, text="req", bot=mock_bot)
    cb_req = make_mock_callback(user=stranger_tg, data="req_access", message=cb_msg, bot=mock_bot)
    await router.propagate_event("callback_query", cb_req, db=db, bot=mock_bot)

    req_id = 1

    # 3. Owner approves request as 'friend'
    mock_bot.send_message.reset_mock()
    owner_tg = TgUser(id=1001, is_bot=False, first_name="Owner")
    cb_approve = make_mock_callback(
        user=owner_tg, data=f"acc_ok:friend:{req_id}", message=cb_msg, bot=mock_bot
    )
    await router.propagate_event("callback_query", cb_approve, db=db, bot=mock_bot)

    # User must be created and active
    created_user = await db.get_user_by_telegram_id(6001)
    assert created_user is not None
    assert created_user["status"] == "active"

    # CRITICAL: Newly approved user MUST receive Onboarding Screen 1 with [ Дальше ➡️ ] button
    mock_bot.send_message.assert_called()
    user_msg_call = None
    for call in mock_bot.send_message.call_args_list:
        if call.kwargs.get("chat_id") == 6001:
            user_msg_call = call
            break

    assert user_msg_call is not None, "Bot did not send message to newly approved user 6001"
    user_text = user_msg_call.kwargs.get("text", "")
    assert "Привет, Иван!" in user_text
    assert "Как это работает" in user_text

    user_markup = user_msg_call.kwargs.get("reply_markup")
    assert user_markup is not None, "Onboarding Screen 1 must include inline keyboard"
    assert user_markup.inline_keyboard[0][0].callback_data == "onb:2"

    # 4. Progress through Screen 2
    cb_onb2_msg = make_mock_message(chat_id=6001, user=stranger_tg, text="screen1", bot=mock_bot)
    cb2 = make_mock_callback(user=stranger_tg, data="onb:2", message=cb_onb2_msg, bot=mock_bot)
    await router.propagate_event("callback_query", cb2, db=db, bot=mock_bot)
    cb_onb2_msg.edit_text.assert_called()
    assert ONBOARDING_SCREEN_2 in cb_onb2_msg.edit_text.call_args.args[0]
    assert (
        cb_onb2_msg.edit_text.call_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
        == "onb:3"
    )

    # 5. Progress through Screen 3
    cb3 = make_mock_callback(user=stranger_tg, data="onb:3", message=cb_onb2_msg, bot=mock_bot)
    await router.propagate_event("callback_query", cb3, db=db, bot=mock_bot)
    cb_onb2_msg.edit_text.assert_called()
    assert "Что я о вас знаю" in cb_onb2_msg.edit_text.call_args.args[0]
    assert (
        cb_onb2_msg.edit_text.call_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
        == "onb:done"
    )

    # 6. Complete onboarding (onb:done)
    mock_bot.send_message.reset_mock()
    cb_done = make_mock_callback(
        user=stranger_tg, data="onb:done", message=cb_onb2_msg, bot=mock_bot
    )
    await router.propagate_event("callback_query", cb_done, db=db, bot=mock_bot)
    cb_onb2_msg.delete.assert_called_once()
    mock_bot.send_message.assert_called()
    final_call = mock_bot.send_message.call_args
    assert final_call.kwargs["chat_id"] == 6001
    assert "Иван" in final_call.kwargs["text"]
    assert final_call.kwargs["reply_markup"] is not None  # main_menu_keyboard
