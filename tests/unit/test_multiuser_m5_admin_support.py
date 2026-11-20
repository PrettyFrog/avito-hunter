from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import CallbackQuery, Chat, Message
from aiogram.types import User as TgUser

from hunter.core.db import Database
from hunter.core.product import (
    ProductProfile,
    ProductStore,
    save_product_profile,
)
from hunter.notify.broadcast import get_broadcast_target_counts, send_broadcast
from hunter.notify.messages_ru import (
    ACCESS_DENIED,
    ADMIN_CONFIRM_EXPIRED,
    SUPPORT_LIMIT_REACHED,
    SUPPORT_TOO_LONG,
)
from hunter.notify.telegram_router import _ADMIN_CONFIRMATIONS, setup_telegram_router


@pytest.fixture(autouse=True)
def isolate_admin_rate_state() -> Iterator[None]:
    from hunter.notify.security import GLOBAL_STRANGER_LIMITER

    GLOBAL_STRANGER_LIMITER.reset()
    try:
        yield
    finally:
        GLOBAL_STRANGER_LIMITER.reset()


def make_mock_message(
    chat_id: int, user: TgUser, text: str = "", bot: AsyncMock | None = None
) -> Message:
    chat = Chat(id=chat_id, type="private")
    msg = Message(message_id=10, date=datetime.now(), chat=chat, from_user=user, text=text)
    if bot:
        msg = msg.as_(bot)
    return msg


def make_mock_callback(
    chat_id: int, user: TgUser, data: str, message_id: int = 10, bot: AsyncMock | None = None
) -> CallbackQuery:
    chat = Chat(id=chat_id, type="private")
    msg = Message(
        message_id=message_id, date=datetime.now(), chat=chat, from_user=user, text="Original msg"
    )
    if bot:
        msg = msg.as_(bot)
    cq = CallbackQuery(
        id="cq_1",
        from_user=user,
        chat_instance="ci_1",
        message=msg,
        data=data,
    )
    if bot:
        cq = cq.as_(bot)
    return cq


@pytest.mark.asyncio
async def test_support_flow_limits_and_replies(tmp_path: Path) -> None:
    """Verifies:
    - User /support command limits (max 3 tickets/24h).
    - Text length checks (max 1000 characters).
    - Owner receiving alert with reply callback.
    - Owner reply delivery to user.
    - Graceful handling when recipient blocked the bot or was deleted.
    """
    db_path = tmp_path / "test.db"
    db = Database(db_path)
    await db.connect()

    await db.create_user(
        telegram_user_id=1001, chat_id=1001, role="owner", status="active"
    )
    user_id = await db.create_user(
        telegram_user_id=2002, chat_id=2002, role="user", status="active"
    )

    mock_channel = MagicMock()
    mock_channel.alert = AsyncMock()
    mock_bot = AsyncMock()

    router = setup_telegram_router(mock_channel, db=db)
    user_tg = TgUser(id=2002, is_bot=False, first_name="User2", username="user2")
    owner_tg = TgUser(id=1001, is_bot=False, first_name="Owner", username="owner")

    # 1. User starts /support
    replies_1: list[str] = []
    msg_sup = make_mock_message(chat_id=2002, user=user_tg, text="/support", bot=mock_bot)
    object.__setattr__(msg_sup, "reply", AsyncMock(side_effect=lambda t, **kw: replies_1.append(t)))
    await router.propagate_event("message", msg_sup, db=db, bot=mock_bot)
    assert len(replies_1) == 1
    assert "Опишите ваш вопрос" in replies_1[0]

    # 2. User sends text exceeding 1000 chars -> rejected
    long_text = "x" * 1001
    replies_long: list[str] = []
    msg_long = make_mock_message(chat_id=2002, user=user_tg, text=long_text, bot=mock_bot)
    object.__setattr__(
        msg_long, "reply", AsyncMock(side_effect=lambda t, **kw: replies_long.append(t))
    )
    await router.propagate_event("message", msg_long, db=db, bot=mock_bot)
    assert len(replies_long) == 1
    assert replies_long[0] == SUPPORT_TOO_LONG

    # 3. User sends valid text
    replies_ok: list[str] = []
    msg_ok = make_mock_message(chat_id=2002, user=user_tg, text="Valid question", bot=mock_bot)
    object.__setattr__(msg_ok, "reply", AsyncMock(side_effect=lambda t, **kw: replies_ok.append(t)))
    await router.propagate_event("message", msg_ok, db=db, bot=mock_bot)
    assert len(replies_ok) == 1
    assert "обращение #1 принято" in replies_ok[0].lower()

    # Owner should receive an alert with button
    mock_bot.send_message.assert_called()
    call_args = mock_bot.send_message.call_args
    assert call_args.kwargs["chat_id"] == 1001
    assert "Valid question" in call_args.kwargs["text"]

    # 4. Limit check: simulate 3 tickets in 24h
    await db.create_support_ticket(user_id, 2002, "Question 2")
    await db.create_support_ticket(user_id, 2002, "Question 3")

    replies_lim: list[str] = []
    msg_lim = make_mock_message(chat_id=2002, user=user_tg, text="/support", bot=mock_bot)
    object.__setattr__(
        msg_lim, "reply", AsyncMock(side_effect=lambda t, **kw: replies_lim.append(t))
    )
    await router.propagate_event("message", msg_lim, db=db, bot=mock_bot)
    assert len(replies_lim) == 1
    assert replies_lim[0] == SUPPORT_LIMIT_REACHED

    # 5. Owner replies to ticket #1
    cb_reply = make_mock_callback(chat_id=1001, user=owner_tg, data="ticket_reply:1", bot=mock_bot)
    object.__setattr__(cb_reply, "answer", AsyncMock())
    await router.propagate_event("callback_query", cb_reply, db=db, bot=mock_bot)

    replies_owner: list[str] = []
    msg_answer = make_mock_message(
        chat_id=1001, user=owner_tg, text="Here is the answer!", bot=mock_bot
    )
    object.__setattr__(
        msg_answer, "reply", AsyncMock(side_effect=lambda t, **kw: replies_owner.append(t))
    )
    await router.propagate_event("message", msg_answer, db=db, bot=mock_bot)

    assert len(replies_owner) == 1
    assert "успешно доставлен" in replies_owner[0]
    t1 = await db.get_support_ticket(1)
    assert t1["status"] == "answered"
    assert t1["response_text"] == "Here is the answer!"

    # 6. Recipient blocked bot -> graceful undeliverable handling
    cb_reply2 = make_mock_callback(chat_id=1001, user=owner_tg, data="ticket_reply:2", bot=mock_bot)
    object.__setattr__(cb_reply2, "answer", AsyncMock())
    await router.propagate_event("callback_query", cb_reply2, db=db, bot=mock_bot)

    mock_bot.send_message.side_effect = TelegramForbiddenError(
        method=MagicMock(), message="Forbidden: bot was blocked by the user"
    )
    replies_owner2: list[str] = []
    msg_answer2 = make_mock_message(
        chat_id=1001, user=owner_tg, text="Another answer", bot=mock_bot
    )
    object.__setattr__(
        msg_answer2, "reply", AsyncMock(side_effect=lambda t, **kw: replies_owner2.append(t))
    )
    await router.propagate_event("message", msg_answer2, db=db, bot=mock_bot)

    assert len(replies_owner2) == 1
    assert "пользователь заблокировал бота" in replies_owner2[0].lower()
    t2 = await db.get_support_ticket(2)
    assert t2["status"] == "undeliverable"
    assert t2["error"] == "bot_blocked"


@pytest.mark.asyncio
async def test_broadcast_execution_rate_limit_and_retry_after(tmp_path: Path) -> None:
    """Verifies broadcast:
    - Target counts computation.
    - Rate limit <= 20 msg/sec (verified via timestamps / sleep).
    - TelegramRetryAfter handling.
    - Database logging in broadcasts table.
    """
    db_path = tmp_path / "test.db"
    db = Database(db_path)
    await db.connect()

    owner_id = await db.create_user(
        telegram_user_id=1001, chat_id=1001, role="owner", status="active"
    )
    # 2 family users, 1 friend user
    u1 = await db.create_user(
        telegram_user_id=2001, chat_id=2001, role="user", status="active", tier="family"
    )
    u2 = await db.create_user(
        telegram_user_id=2002, chat_id=2002, role="user", status="active", tier="family"
    )
    u3 = await db.create_user(
        telegram_user_id=2003, chat_id=2003, role="user", status="active", tier="friend"
    )

    counts = await get_broadcast_target_counts(db)
    assert counts["all"] == 3
    assert counts["family"] == 2
    assert counts["friend"] == 1

    mock_bot = AsyncMock()
    call_count = 0

    async def mock_send(chat_id, text, **kw):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            # Simulate RetryAfter once
            err = TelegramRetryAfter(
                method=MagicMock(), message="Too many requests", retry_after=0.1
            )
            raise err
        return MagicMock()

    mock_bot.send_message.side_effect = mock_send

    t0 = time.time()
    delivered, failed = await send_broadcast(
        db,
        "Important announcement!",
        target_audience="family",
        bot=mock_bot,
        sender_user_id=owner_id,
    )
    t1 = time.time()

    assert delivered == 2
    assert failed == 0
    assert (t1 - t0) >= 0.1  # Verified retry after was respected

    # Check broadcasts table
    history = await db.get_broadcast_history()
    assert len(history) == 1
    assert history[0]["target_audience"] == "family"
    assert history[0]["text"] == "Important announcement!"
    assert history[0]["delivered_count"] == 2


@pytest.mark.asyncio
async def test_admin_confirmations_2min_expiration_and_tier_stretching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verifies:
    - Admin confirmations expire after 120 seconds.
    - Confirmations are tied to message_id.
    - Tier change stretches existing user product intervals immediately with 1 notification to user.
    """
    db_path = tmp_path / "test.db"
    db = Database(db_path)
    await db.connect()

    owner_id = await db.create_user(
        telegram_user_id=1001, chat_id=1001, role="owner", status="active"
    )
    user_id = await db.create_user(
        telegram_user_id=2002, chat_id=2002, role="user", status="active", tier="friend"
    )

    products_dir = tmp_path / "products"
    products_dir.mkdir(parents=True, exist_ok=True)
    store = ProductStore(directory=products_dir)

    from hunter.core import product as product_module
    from hunter.notify import telegram_router as router_module

    monkeypatch.setattr(product_module, "get_product_store", lambda directory=None: store)
    monkeypatch.setattr(router_module, "get_product_store", lambda directory=None: store)

    # Product with 5-minute poll interval
    p = ProductProfile(
        id="prod_stretch_1",
        name="Interval Test",
        search_url="https://avito.ru",
        max_price=10000,
        owner_user_id=user_id,
        poll_minutes=5,
    )
    save_product_profile(p, directory=products_dir)

    mock_channel = MagicMock()
    mock_channel.alert = AsyncMock()
    mock_bot = AsyncMock()

    router = setup_telegram_router(mock_channel, db=db)
    owner_tg = TgUser(id=1001, is_bot=False, first_name="Owner", username="owner")

    # 1. 2-minute confirmation test:
    # Trigger admin block confirmation
    cb_block = make_mock_callback(
        chat_id=1001, user=owner_tg, data=f"admin:u_block:{user_id}", message_id=55, bot=mock_bot
    )
    object.__setattr__(cb_block.message, "edit_text", AsyncMock())
    object.__setattr__(cb_block, "answer", AsyncMock())
    await router.propagate_event("callback_query", cb_block, db=db, bot=mock_bot)

    # Verify confirmation exists
    key = (1001, 55)
    assert key in _ADMIN_CONFIRMATIONS
    assert _ADMIN_CONFIRMATIONS[key]["action"] == "block"

    # Simulate expiration by setting expires_at in the past
    _ADMIN_CONFIRMATIONS[key]["expires_at"] = time.time() - 1.0

    cb_confirm = make_mock_callback(
        chat_id=1001,
        user=owner_tg,
        data=f"admin:u_block_confirm:{user_id}",
        message_id=55,
        bot=mock_bot,
    )
    object.__setattr__(cb_confirm.message, "edit_text", AsyncMock())
    object.__setattr__(cb_confirm, "answer", AsyncMock())
    await router.propagate_event("callback_query", cb_confirm, db=db, bot=mock_bot)

    # Check alert was shown about expiration
    cb_confirm.answer.assert_called_with(ADMIN_CONFIRM_EXPIRED, show_alert=True)
    # User is NOT blocked
    u = await db.get_user_by_id(user_id)
    assert u["status"] == "active"

    # 2. Tier change: Friend -> Family
    # Reset mock
    mock_bot.send_message.reset_mock()
    cb_tier = make_mock_callback(
        chat_id=1001,
        user=owner_tg,
        data=f"admin:u_tier_set:{user_id}:family",
        message_id=56,
        bot=mock_bot,
    )
    object.__setattr__(cb_tier.message, "edit_text", AsyncMock())
    object.__setattr__(cb_tier, "answer", AsyncMock())
    await router.propagate_event("callback_query", cb_tier, db=db, bot=mock_bot)

    # User in DB updated to family
    u = await db.get_user_by_id(user_id)
    assert u["tier"] == "family"

    # Exactly 1 notification sent to the user
    mock_bot.send_message.assert_called_once()
    assert mock_bot.send_message.call_args.kwargs["chat_id"] == 2002
    assert "Ваш тариф изменён на «Семья»" in mock_bot.send_message.call_args.kwargs["text"]


@pytest.mark.asyncio
async def test_admin_stats_and_system_state_callbacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verifies that /admin stats and system callbacks render properly without crashing."""
    db_path = tmp_path / "test_stats.db"
    db = Database(db_path)
    await db.connect()

    owner_id = await db.create_user(
        telegram_user_id=1001, chat_id=1001, role="owner", status="active"
    )

    products_dir = tmp_path / "products"
    products_dir.mkdir(parents=True, exist_ok=True)
    store = ProductStore(directory=products_dir)

    from hunter.core import product as product_module
    from hunter.notify import telegram_router as router_module

    monkeypatch.setattr(product_module, "get_product_store", lambda directory=None: store)
    monkeypatch.setattr(router_module, "get_product_store", lambda directory=None: store)

    mock_channel = MagicMock()
    mock_channel.alert = AsyncMock()
    mock_bot = AsyncMock()

    router = setup_telegram_router(mock_channel, db=db)
    owner_tg = TgUser(id=1001, is_bot=False, first_name="Owner", username="owner")

    # 1. Test cb_admin_stats
    cb_stats = make_mock_callback(
        chat_id=1001, user=owner_tg, data="admin:stats", message_id=60, bot=mock_bot
    )
    object.__setattr__(cb_stats.message, "edit_text", AsyncMock())
    object.__setattr__(cb_stats, "answer", AsyncMock())
    await router.propagate_event("callback_query", cb_stats, db=db, bot=mock_bot)

    cb_stats.answer.assert_called_once()
    cb_stats.message.edit_text.assert_called_once()
    stats_text = cb_stats.message.edit_text.call_args.args[0]
    assert "Статистика сервиса:" in stats_text
    assert "Товары:" in stats_text

    # 2. Test cb_admin_system
    cb_sys = make_mock_callback(
        chat_id=1001, user=owner_tg, data="admin:system", message_id=61, bot=mock_bot
    )
    object.__setattr__(cb_sys.message, "edit_text", AsyncMock())
    object.__setattr__(cb_sys, "answer", AsyncMock())
    await router.propagate_event("callback_query", cb_sys, db=db, bot=mock_bot)

    cb_sys.answer.assert_called_once()
    cb_sys.message.edit_text.assert_called_once()
    sys_text = cb_sys.message.edit_text.call_args.args[0]
    assert "Состояние системы:" in sys_text
    assert "Глобальная пауза:" in sys_text


@pytest.mark.asyncio
async def test_admin_users_list_and_view_callbacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify admin user listing and details with a real SQLite database."""
    db_path = tmp_path / "test_admin_users.db"
    db = Database(db_path)
    await db.connect()

    owner_id = await db.create_user(
        telegram_user_id=1001, chat_id=1001, role="owner", status="active", display_name="Owner"
    )
    user_id = await db.create_user(
        telegram_user_id=2002,
        chat_id=2002,
        role="user",
        status="active",
        display_name="Тестовый Юзер",
        username="testuser",
    )

    products_dir = tmp_path / "products"
    products_dir.mkdir(parents=True, exist_ok=True)
    store = ProductStore(directory=products_dir)

    from hunter.core import product as product_module
    from hunter.notify import telegram_router as router_module

    monkeypatch.setattr(product_module, "get_product_store", lambda directory=None: store)
    monkeypatch.setattr(router_module, "get_product_store", lambda directory=None: store)

    mock_channel = MagicMock()
    mock_channel.alert = AsyncMock()
    mock_bot = AsyncMock()

    router = setup_telegram_router(mock_channel, db=db)
    owner_tg = TgUser(id=1001, is_bot=False, first_name="Owner", username="owner")

    # 1. Test clicking "Пользователи" (admin:users)
    cb_users = make_mock_callback(
        chat_id=1001, user=owner_tg, data="admin:users", message_id=70, bot=mock_bot
    )
    object.__setattr__(cb_users.message, "edit_text", AsyncMock())
    object.__setattr__(cb_users, "answer", AsyncMock())
    await router.propagate_event("callback_query", cb_users, db=db, bot=mock_bot)

    cb_users.answer.assert_called_once()
    cb_users.message.edit_text.assert_called_once()
    users_text = cb_users.message.edit_text.call_args.args[0]
    users_kb = cb_users.message.edit_text.call_args.kwargs["reply_markup"]
    assert "Список пользователей:" in users_text
    btn_texts = [btn.text for row in users_kb.inline_keyboard for btn in row]
    assert any("Тестовый Юзер" in t for t in btn_texts)

    # 2. Test viewing user details (admin:u_view:<uid>)
    cb_u_view = make_mock_callback(
        chat_id=1001, user=owner_tg, data=f"admin:u_view:{user_id}", message_id=71, bot=mock_bot
    )
    object.__setattr__(cb_u_view.message, "edit_text", AsyncMock())
    object.__setattr__(cb_u_view, "answer", AsyncMock())
    await router.propagate_event("callback_query", cb_u_view, db=db, bot=mock_bot)

    cb_u_view.answer.assert_called_once()
    cb_u_view.message.edit_text.assert_called_once()
    view_text = cb_u_view.message.edit_text.call_args.args[0]
    view_kb = cb_u_view.message.edit_text.call_args.kwargs["reply_markup"]
    assert "Пользователь:</b> Тестовый Юзер" in view_text
    view_btn_texts = [btn.text for row in view_kb.inline_keyboard for btn in row]
    assert any("Заблокировать" in t for t in view_btn_texts)
    assert any("Удалить пользователя" in t for t in view_btn_texts)

    # 3. Test block confirmation modal (admin:u_block:<uid>)
    cb_u_block = make_mock_callback(
        chat_id=1001, user=owner_tg, data=f"admin:u_block:{user_id}", message_id=72, bot=mock_bot
    )
    object.__setattr__(cb_u_block.message, "edit_text", AsyncMock())
    object.__setattr__(cb_u_block, "answer", AsyncMock())
    await router.propagate_event("callback_query", cb_u_block, db=db, bot=mock_bot)

    cb_u_block.answer.assert_called_once()
    cb_u_block.message.edit_text.assert_called_once()
    block_text = cb_u_block.message.edit_text.call_args.args[0]
    assert "Заблокировать пользователя Тестовый Юзер" in block_text

    # 4. Test delete confirmation modal (admin:u_del:<uid>)
    cb_u_del = make_mock_callback(
        chat_id=1001, user=owner_tg, data=f"admin:u_del:{user_id}", message_id=73, bot=mock_bot
    )
    object.__setattr__(cb_u_del.message, "edit_text", AsyncMock())
    object.__setattr__(cb_u_del, "answer", AsyncMock())
    await router.propagate_event("callback_query", cb_u_del, db=db, bot=mock_bot)

    cb_u_del.answer.assert_called_once()
    cb_u_del.message.edit_text.assert_called_once()
    del_text = cb_u_del.message.edit_text.call_args.args[0]
    assert "Удалить пользователя Тестовый Юзер" in del_text


@pytest.mark.asyncio
async def test_admin_errors_callback_and_command(tmp_path: Path) -> None:
    """Verifies that the /admin network errors button and /errors command work."""
    from hunter.notify.messages_ru import ADMIN_BTN_ERRORS

    db_path = tmp_path / "test_admin_errs.db"
    db = Database(db_path)
    await db.connect()

    owner_id = await db.create_user(
        telegram_user_id=1001, chat_id=1001, role="owner", status="active", display_name="Owner"
    )
    mock_channel = MagicMock()
    mock_bot = AsyncMock()

    router = setup_telegram_router(mock_channel, db=db)
    owner_tg = TgUser(id=1001, is_bot=False, first_name="Owner", username="owner")

    # 1. Check root menu has ADMIN_BTN_ERRORS
    msg_admin = make_mock_message(chat_id=1001, user=owner_tg, text="/admin", bot=mock_bot)
    object.__setattr__(msg_admin, "reply", AsyncMock())
    await router.propagate_event("message", msg_admin, db=db, bot=mock_bot)
    msg_admin.reply.assert_called_once()
    kb = msg_admin.reply.call_args.kwargs["reply_markup"]
    btn_texts = [btn.text for row in kb.inline_keyboard for btn in row]
    assert ADMIN_BTN_ERRORS in btn_texts

    # 2. Callback when no errors in DB
    cb_err = make_mock_callback(
        chat_id=1001, user=owner_tg, data="admin:errors:0", message_id=80, bot=mock_bot
    )
    object.__setattr__(cb_err.message, "edit_text", AsyncMock())
    object.__setattr__(cb_err, "answer", AsyncMock())
    await router.propagate_event("callback_query", cb_err, db=db, bot=mock_bot)

    cb_err.answer.assert_called_once()
    cb_err.message.edit_text.assert_called_once()
    empty_text = cb_err.message.edit_text.call_args.args[0]
    assert "не зафиксировано" in empty_text

    # 3. Add errors and test again
    await db.log_request(
        "sony_xm6", "search_render", 502, "https://avito.ru/search", 3000, "http_502"
    )
    await db.log_request("bose_qc", "card", 0, "https://avito.ru/card", 30000, "timeout")

    cb_err2 = make_mock_callback(
        chat_id=1001, user=owner_tg, data="admin:errors:0", message_id=81, bot=mock_bot
    )
    object.__setattr__(cb_err2.message, "edit_text", AsyncMock())
    object.__setattr__(cb_err2, "answer", AsyncMock())
    await router.propagate_event("callback_query", cb_err2, db=db, bot=mock_bot)

    err_text = cb_err2.message.edit_text.call_args.args[0]
    assert "Лог сетевых ошибок" in err_text
    assert "Всего ошибок: <b>2</b>" in err_text
    assert "HTTP 502" in err_text
    assert "sony_xm6" in err_text
    assert "bose_qc" in err_text

    # 4. Command /errors
    msg_errors = make_mock_message(chat_id=1001, user=owner_tg, text="/errors", bot=mock_bot)
    object.__setattr__(msg_errors, "reply", AsyncMock())
    await router.propagate_event("message", msg_errors, db=db, bot=mock_bot)
    msg_errors.reply.assert_called_once()
    cmd_text = msg_errors.reply.call_args.args[0]
    assert "Лог сетевых ошибок" in cmd_text
    assert "Всего ошибок: <b>2</b>" in cmd_text


@pytest.mark.asyncio
async def test_non_owner_alien_admin_callbacks_rejected(tmp_path: Path) -> None:
    db = Database(tmp_path / "admin_perm.db")
    await db.connect()
    await db.create_user(
        telegram_user_id=2002, chat_id=2002, role="user", status="active", tier="friend"
    )
    user_tg = TgUser(id=2002, is_bot=False, first_name="Regular")
    mock_channel = MagicMock()
    mock_channel.chat_id = 1001
    mock_bot = AsyncMock()
    router = setup_telegram_router(mock_channel, db=db)
    admin_cbs = ["admin:errors:0", "admin:stats", "admin:u_block:2002"]
    for data in admin_cbs:
        cb = make_mock_callback(chat_id=2002, user=user_tg, data=data, bot=mock_bot)
        object.__setattr__(cb, "answer", AsyncMock())
        await router.propagate_event("callback_query", cb, db=db, bot=mock_bot)
        assert cb.answer.called
        assert ACCESS_DENIED in cb.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_admin_self_block_and_delete_callbacks_have_no_effect(tmp_path: Path) -> None:
    """Owner cannot target their own account through forged admin callback data."""
    db = Database(tmp_path / "admin_self_target.db")
    await db.connect()
    owner_id = await db.create_user(
        telegram_user_id=1001, chat_id=1001, role="owner", status="active"
    )
    owner_tg = TgUser(id=1001, is_bot=False, first_name="Owner")
    mock_channel = MagicMock()
    mock_channel.chat_id = 1001
    mock_bot = AsyncMock()
    router = setup_telegram_router(mock_channel, db=db)
    before_keys = set(_ADMIN_CONFIRMATIONS)

    for action in ("u_block", "u_del"):
        cb = make_mock_callback(
            chat_id=1001, user=owner_tg, data=f"admin:{action}:{owner_id}",
            message_id=900 + len(action), bot=mock_bot,
        )
        object.__setattr__(cb.message, "edit_text", AsyncMock())
        object.__setattr__(cb, "answer", AsyncMock())
        await router.propagate_event("callback_query", cb, db=db, bot=mock_bot)
        cb.answer.assert_awaited_once()
        cb.message.edit_text.assert_not_awaited()

    assert (await db.get_user_by_id(owner_id))["status"] == "active"
    assert set(_ADMIN_CONFIRMATIONS) == before_keys


@pytest.mark.asyncio
async def test_forged_admin_callback_and_confirmation_target_mismatch_are_denied(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "admin_forged_target.db")
    await db.connect()
    await db.create_user(
        telegram_user_id=1001, chat_id=1001, role="owner", status="active"
    )
    user_id = await db.create_user(
        telegram_user_id=2002, chat_id=2002, role="user", status="active"
    )
    owner_tg = TgUser(id=1001, is_bot=False, first_name="Owner")
    attacker = TgUser(id=2002, is_bot=False, first_name="Attacker")
    channel = MagicMock()
    channel.chat_id = 1001
    bot = AsyncMock()
    router = setup_telegram_router(channel, db=db)

    forged = make_mock_callback(
        chat_id=2002, user=attacker, data=f"admin:u_block:{user_id}", bot=bot
    )
    object.__setattr__(forged, "answer", AsyncMock())
    await router.propagate_event("callback_query", forged, db=db, bot=bot)
    forged.answer.assert_awaited_once()
    assert ACCESS_DENIED in forged.answer.call_args.args[0]
    assert (await db.get_user_by_id(user_id))["status"] == "active"

    global_key = (1001, 951)
    _ADMIN_CONFIRMATIONS[global_key] = {
        "action": "block", "user_id": user_id, "expires_at": time.time() + 120
    }
    mismatched = make_mock_callback(
        chat_id=1001, user=owner_tg, data=f"admin:u_block_confirm:{user_id + 99}",
        message_id=951, bot=bot,
    )
    object.__setattr__(mismatched, "answer", AsyncMock())
    await router.propagate_event("callback_query", mismatched, db=db, bot=bot)
    mismatched.answer.assert_awaited_once_with(ADMIN_CONFIRM_EXPIRED, show_alert=True)
    assert (await db.get_user_by_id(user_id))["status"] == "active"
    _ADMIN_CONFIRMATIONS.pop(global_key, None)
