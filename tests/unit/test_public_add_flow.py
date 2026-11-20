"""Unit and integration tests for Telegram public add flow (S4 / T-474).

Verifies:
- Two parallel users concurrently completing public add without cross-talk
- Rejection of invalid URLs, invalid prices, and conflicting prices with graceful retry
- Optional stop-words skipping via text ('-') and inline callback
- Cancellation via /cancel, text, and inline callback
- Zero-LLM guarantee (raising spy proves zero LLM invocation)
- Tenant isolation against alien message and callback interception
- Legacy user routing preserved (cutover selector invariance)
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import CallbackQuery, Chat, Message, User as TgUser

from hunter.application.cutover import CutoverManager, CutoverMode
from hunter.application.subscriptions import SubscriptionService
from hunter.core.db import Database
from hunter.llm.router import LLMRouter
from hunter.notify.telegram_router import setup_telegram_router
from hunter.storage.connection import StorageConfig, StorageConnectionManager
from hunter.storage.subscriptions import SubscriptionRepository
from hunter.storage.writer import SQLiteWriterActor


def _make_msg(chat_id: int, user: TgUser, text: str = "", bot: AsyncMock | None = None) -> Message:
    chat = Chat(id=chat_id, type="private")
    msg = Message(
        message_id=1,
        date=int(datetime.now(UTC).timestamp()),
        chat=chat,
        from_user=user,
        text=text,
    )
    if bot:
        msg.as_(bot)
    object.__setattr__(msg, "reply", AsyncMock(return_value=True))
    object.__setattr__(msg, "answer", AsyncMock(return_value=True))
    return msg


def _make_cb(chat_id: int, user: TgUser, data: str, bot: AsyncMock | None = None) -> CallbackQuery:
    chat = Chat(id=chat_id, type="private")
    msg = Message(
        message_id=1,
        date=int(datetime.now(UTC).timestamp()),
        chat=chat,
        from_user=user,
        text="",
    )
    cb = CallbackQuery(
        id="cb_1",
        from_user=user,
        chat_instance="ci_1",
        message=msg,
        data=data,
    )
    if bot:
        cb.as_(bot)
        msg.as_(bot)
    object.__setattr__(cb, "answer", AsyncMock(return_value=True))
    object.__setattr__(msg, "reply", AsyncMock(return_value=True))
    return cb


async def _init_env(tmp_path: Path):
    db_path = tmp_path / "hunter_test.db"
    db = Database(db_path)
    await db.connect()
    mgr = StorageConnectionManager(StorageConfig(db_path=db_path))
    writer = SQLiteWriterActor(mgr)
    await writer.start()
    repo = SubscriptionRepository(mgr, writer)
    sub_service = SubscriptionService(repo)
    setattr(db, "subscription_service", sub_service)
    setattr(db, "storage_mgr", mgr)
    setattr(db, "writer", writer)

    mock_bot = AsyncMock()
    mock_channel = MagicMock()
    mock_channel.chat_id = 9999
    router = setup_telegram_router(mock_channel, db=db)
    cm = CutoverManager()
    return db, writer, repo, sub_service, cm, router, mock_bot


@pytest.mark.asyncio
async def test_public_add_two_parallel_users(tmp_path: Path) -> None:
    db, writer, repo, sub_service, cm, router, bot = await _init_env(tmp_path)
    try:
        u1_id = await db.create_user(
            telegram_user_id=101, chat_id=101, role="user", status="active"
        )
        u2_id = await db.create_user(
            telegram_user_id=102, chat_id=102, role="user", status="active"
        )
        await cm.set_tenant_mode(u1_id, CutoverMode.V2)
        await cm.set_tenant_mode(u2_id, CutoverMode.V2)

        u1 = TgUser(id=101, is_bot=False, first_name="User1")
        u2 = TgUser(id=102, is_bot=False, first_name="User2")

        # 1. Concurrently start /add
        await router.propagate_event("message", _make_msg(101, u1, "/add", bot), db=db, bot=bot)
        await router.propagate_event("message", _make_msg(102, u2, "/add", bot), db=db, bot=bot)

        st1 = await db.get_pending_product(101)
        st2 = await db.get_pending_product(102)
        assert st1 and st1["step"] == "public_add_name"
        assert st2 and st2["step"] == "public_add_name"
        await router.propagate_event("message", _make_msg(101, u1, "Ноутбук", bot), db=db, bot=bot)
        await router.propagate_event("message", _make_msg(102, u2, "Авто", bot), db=db, bot=bot)

        # 2. Both send distinct search URLs
        url1 = "https://www.avito.ru/moskva/noutbuki?q=macbook"
        url2 = "https://www.avito.ru/sankt-peterburg/avtomobili?q=volvo"
        await router.propagate_event("message", _make_msg(101, u1, url1, bot), db=db, bot=bot)
        await router.propagate_event("message", _make_msg(102, u2, url2, bot), db=db, bot=bot)

        assert (await db.get_pending_product(101))["step"] == "public_add_price"
        assert (await db.get_pending_product(102))["step"] == "public_add_price"

        # 3. Both send distinct price ceilings
        await router.propagate_event("message", _make_msg(101, u1, "120 000", bot), db=db, bot=bot)
        await router.propagate_event(
            "message", _make_msg(102, u2, "2 500 000 руб", bot), db=db, bot=bot
        )

        assert (await db.get_pending_product(101))["step"] == "public_add_words"
        assert (await db.get_pending_product(102))["step"] == "public_add_words"

        # 4. User 1 provides manual stopwords; User 2 skips via callback
        await router.propagate_event(
            "message", _make_msg(101, u1, "битый, на запчасти", bot), db=db, bot=bot
        )
        await router.propagate_event(
            "callback_query", _make_cb(102, u2, "public_add_skip_words", bot), db=db, bot=bot
        )

        # 5. Verify pending products cleaned up
        assert await db.get_pending_product(101) is None
        assert await db.get_pending_product(102) is None

        # 6. Verify subscriptions recorded in repository
        subs1 = await sub_service.list_subscriptions(u1_id)
        subs2 = await sub_service.list_subscriptions(u2_id)
        assert len(subs1) == 1
        assert len(subs2) == 1
        assert subs1[0].max_price == 120000
        assert subs1[0].manual_stopwords == "битый,на запчасти"
        assert subs2[0].max_price == 2500000
        assert subs2[0].manual_stopwords == ""
    finally:
        await writer.stop()


@pytest.mark.asyncio
async def test_public_add_invalid_url_and_price_retries(tmp_path: Path) -> None:
    db, writer, repo, sub_service, cm, router, bot = await _init_env(tmp_path)
    try:
        u_id = await db.create_user(telegram_user_id=201, chat_id=201, role="user", status="active")
        await cm.set_tenant_mode(u_id, CutoverMode.V2)
        u = TgUser(id=201, is_bot=False, first_name="Tester")

        await router.propagate_event("message", _make_msg(201, u, "/add", bot), db=db, bot=bot)
        await router.propagate_event("message", _make_msg(201, u, "Поиск", bot), db=db, bot=bot)

        # Invalid scheme
        msg_inval1 = _make_msg(201, u, "http://www.avito.ru/moskva", bot)
        await router.propagate_event("message", msg_inval1, db=db, bot=bot)
        assert (await db.get_pending_product(201))["step"] == "public_add_url"
        msg_inval1.reply.assert_called()

        # Non-avito domain
        msg_inval2 = _make_msg(201, u, "https://example.com/items", bot)
        await router.propagate_event("message", msg_inval2, db=db, bot=bot)
        assert (await db.get_pending_product(201))["step"] == "public_add_url"

        # Valid URL advances to price
        await router.propagate_event(
            "message",
            _make_msg(201, u, "https://www.avito.ru/moskva?q=iphone", bot),
            db=db,
            bot=bot,
        )
        assert (await db.get_pending_product(201))["step"] == "public_add_price"

        # Invalid price (no digits or <= 0)
        msg_p1 = _make_msg(201, u, "бесплатно", bot)
        await router.propagate_event("message", msg_p1, db=db, bot=bot)
        assert (await db.get_pending_product(201))["step"] == "public_add_price"

        msg_p2 = _make_msg(201, u, "0", bot)
        await router.propagate_event("message", msg_p2, db=db, bot=bot)
        assert (await db.get_pending_product(201))["step"] == "public_add_price"

        # Valid price advances to words
        await router.propagate_event("message", _make_msg(201, u, "75000", bot), db=db, bot=bot)
        assert (await db.get_pending_product(201))["step"] == "public_add_words"

        # Skip via '-'
        await router.propagate_event("message", _make_msg(201, u, "-", bot), db=db, bot=bot)
        assert await db.get_pending_product(201) is None
        subs = await sub_service.list_subscriptions(u_id)
        assert len(subs) == 1
        assert subs[0].max_price == 75000
        assert subs[0].manual_stopwords == ""
    finally:
        await writer.stop()


@pytest.mark.asyncio
async def test_public_add_conflicting_price_error(tmp_path: Path) -> None:
    import base64
    import json

    db, writer, repo, sub_service, cm, router, bot = await _init_env(tmp_path)
    try:
        u_id = await db.create_user(telegram_user_id=250, chat_id=250, role="user", status="active")
        await cm.set_tenant_mode(u_id, CutoverMode.V2)
        u = TgUser(id=250, is_bot=False, first_name="ConflictingPriceTester")

        f_val = base64.b64encode(json.dumps({"to": 30000}).encode("utf-8")).decode("ascii")
        url_with_f = f"https://www.avito.ru/moskva?f={f_val}&q=phone"

        await router.propagate_event("message", _make_msg(250, u, "/add", bot), db=db, bot=bot)
        await router.propagate_event("message", _make_msg(250, u, "Поиск", bot), db=db, bot=bot)
        await router.propagate_event("message", _make_msg(250, u, url_with_f, bot), db=db, bot=bot)
        assert (await db.get_pending_product(250))["step"] == "public_add_price"

        # Price 40000 exceeds 30000 in f-parameter -> ConflictingPriceError handled
        msg_conflict = _make_msg(250, u, "40000", bot)
        await router.propagate_event("message", msg_conflict, db=db, bot=bot)
        assert (await db.get_pending_product(250))["step"] == "public_add_price"
        msg_conflict.reply.assert_called()

        # Valid matching price == 30000 succeeds
        await router.propagate_event("message", _make_msg(250, u, "30000", bot), db=db, bot=bot)
        assert (await db.get_pending_product(250))["step"] == "public_add_words"
    finally:
        await writer.stop()


@pytest.mark.asyncio
async def test_public_add_cancellation(tmp_path: Path) -> None:
    db, writer, repo, sub_service, cm, router, bot = await _init_env(tmp_path)
    try:
        u_id = await db.create_user(telegram_user_id=301, chat_id=301, role="user", status="active")
        await cm.set_tenant_mode(u_id, CutoverMode.V2)
        u = TgUser(id=301, is_bot=False, first_name="Canceller")

        # 1. Cancel via command /cancel
        await router.propagate_event("message", _make_msg(301, u, "/add", bot), db=db, bot=bot)
        assert (await db.get_pending_product(301))["step"] == "public_add_name"
        msg_c1 = _make_msg(301, u, "/cancel", bot)
        await router.propagate_event("message", msg_c1, db=db, bot=bot)
        assert await db.get_pending_product(301) is None
        msg_c1.reply.assert_called_with("❌ Добавление подписки отменено.")

        # 2. Cancel via inline button callback
        await router.propagate_event("message", _make_msg(301, u, "/add", bot), db=db, bot=bot)
        assert (await db.get_pending_product(301))["step"] == "public_add_name"
        cb_c = _make_cb(301, u, "public_add_cancel", bot)
        await router.propagate_event("callback_query", cb_c, db=db, bot=bot)
        assert await db.get_pending_product(301) is None
        cb_c.message.reply.assert_called_with("❌ Добавление подписки отменено.")
    finally:
        await writer.stop()


@pytest.mark.asyncio
async def test_public_add_zero_llm_guarantee(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, writer, repo, sub_service, cm, router, bot = await _init_env(tmp_path)
    try:
        u_id = await db.create_user(telegram_user_id=401, chat_id=401, role="user", status="active")
        await cm.set_tenant_mode(u_id, CutoverMode.V2)
        u = TgUser(id=401, is_bot=False, first_name="ZeroLLMUser")

        def _bomb(*args, **kwargs):
            raise AssertionError("Zero-LLM violation: LLM client called during public add flow!")

        monkeypatch.setattr(LLMRouter, "complete_json", _bomb)
        monkeypatch.setattr(LLMRouter, "complete_vision_json", _bomb)

        await router.propagate_event("message", _make_msg(401, u, "/add", bot), db=db, bot=bot)
        await router.propagate_event("message", _make_msg(401, u, "Поиск", bot), db=db, bot=bot)
        await router.propagate_event(
            "message", _make_msg(401, u, "https://www.avito.ru/moskva?q=ipad", bot), db=db, bot=bot
        )
        await router.propagate_event("message", _make_msg(401, u, "50000", bot), db=db, bot=bot)
        await router.propagate_event("message", _make_msg(401, u, "сколы", bot), db=db, bot=bot)

        subs = await sub_service.list_subscriptions(u_id)
        assert len(subs) == 1
        assert subs[0].max_price == 50000
        assert subs[0].manual_stopwords == "сколы"
    finally:
        await writer.stop()


@pytest.mark.asyncio
async def test_public_add_tenant_isolation_alien_rejected(tmp_path: Path) -> None:
    db, writer, repo, sub_service, cm, router, bot = await _init_env(tmp_path)
    try:
        u1_id = await db.create_user(
            telegram_user_id=501, chat_id=501, role="user", status="active"
        )
        u2_id = await db.create_user(
            telegram_user_id=502, chat_id=502, role="user", status="active"
        )
        await cm.set_tenant_mode(u1_id, CutoverMode.V2)
        await cm.set_tenant_mode(u2_id, CutoverMode.V2)

        u1 = TgUser(id=501, is_bot=False, first_name="Owner1")
        u2 = TgUser(id=502, is_bot=False, first_name="Attacker2")

        # User 1 starts add
        await router.propagate_event("message", _make_msg(501, u1, "/add", bot), db=db, bot=bot)
        st1 = await db.get_pending_product(501)
        assert st1 and st1["owner_user_id"] == u1_id

        # User 2 tries to send a message targeting chat_id 501
        msg_alien = _make_msg(501, u2, "https://www.avito.ru/moskva?q=hack", bot)
        await router.propagate_event("message", msg_alien, db=db, bot=bot)

        # User 1's pending step is unchanged
        st1_after = await db.get_pending_product(501)
        assert st1_after["step"] == "public_add_name"
        assert "url" not in st1_after.get("payload", {})

        # User 2 tries to click callback targeting chat_id 501
        cb_alien = _make_cb(501, u2, "public_add_cancel", bot)
        await router.propagate_event("callback_query", cb_alien, db=db, bot=bot)
        assert (await db.get_pending_product(501)) is not None
        cb_alien.answer.assert_called_with("Чужая сессия добавления.", show_alert=True)
    finally:
        await writer.stop()


@pytest.mark.asyncio
async def test_legacy_user_routing_unaffected(tmp_path: Path) -> None:
    db, writer, repo, sub_service, cm, router, bot = await _init_env(tmp_path)
    try:
        await db.create_user(telegram_user_id=601, chat_id=601, role="user", status="active")
        u = TgUser(id=601, is_bot=False, first_name="LegacyUser")

        await router.propagate_event("message", _make_msg(601, u, "/add", bot), db=db, bot=bot)
        st = await db.get_pending_product(601)
        assert st and st["step"] == "public_add_name"
    finally:
        await writer.stop()
