"""Tests verifying that admin user list and view correctly count Public V2 subscriptions."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import CallbackQuery, Chat, Message
from aiogram.types import User as TgUser

from hunter.core.config import get_settings
from hunter.core.db import Database
from hunter.core.product import ProductProfile, ProductStore, save_product_profile
from hunter.notify.telegram_router import setup_telegram_router


def make_mock_callback(
    chat_id: int, user: TgUser, data: str, message_id: int = 1, bot: AsyncMock | None = None
) -> CallbackQuery:
    chat = Chat(id=chat_id, type="private")
    msg = Message(message_id=message_id, date=datetime.now(), chat=chat, from_user=user, text="")
    cb = CallbackQuery(id="cb_test", from_user=user, chat_instance="inst", message=msg, data=data)
    if bot:
        cb = cb.as_(bot)
    return cb


async def _setup_test_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Database, int, Any, AsyncMock, TgUser]:
    settings = get_settings()
    monkeypatch.setattr(settings.public_v2, "enabled", True)
    db = Database(tmp_path / "test_hunter.db")
    await db.connect()
    await db.create_user(telegram_user_id=1001, chat_id=1001, role="owner", status="active", username="owner")
    user_id = await db.create_user(
        telegram_user_id=2002, chat_id=2002, role="user", status="active", username="gomerdoggo2", display_name="Егор"
    )
    store = ProductStore(directory=tmp_path / "products")
    p1 = ProductProfile(
        id="u1_iphone_17_256", title="iPhone 17", search_url="https://avito.ru/test1",
        max_price=100000, target_price=100000, owner_user_id=user_id,
    )
    save_product_profile(p1, directory=tmp_path / "products")
    store.load_all()
    from hunter.notify import telegram_router as router_module
    from hunter.core import product as product_module
    monkeypatch.setattr(product_module, "get_product_store", lambda directory=None: store)
    monkeypatch.setattr(router_module, "get_product_store", lambda directory=None: store)
    mock_channel, mock_bot = MagicMock(), AsyncMock()
    router = setup_telegram_router(mock_channel, db=db)
    owner_tg = TgUser(id=1001, is_bot=False, first_name="Owner", username="owner")
    return db, user_id, router, mock_bot, owner_tg


@pytest.mark.asyncio
async def test_admin_user_view_v2_zero_subscriptions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db, user_id, router, mock_bot, owner_tg = await _setup_test_env(tmp_path, monkeypatch)

    cb_users = make_mock_callback(chat_id=1001, user=owner_tg, data="admin:users", bot=mock_bot)
    object.__setattr__(cb_users.message, "edit_text", AsyncMock())
    object.__setattr__(cb_users, "answer", AsyncMock())
    await router.propagate_event("callback_query", cb_users, db=db, bot=mock_bot)

    users_kb = cb_users.message.edit_text.call_args.kwargs["reply_markup"]
    btn_texts = [btn.text for row in users_kb.inline_keyboard for btn in row]
    assert any("0 тов." in t and "Егор" in t for t in btn_texts), f"Expected '0 тов.' in buttons: {btn_texts}"

    cb_u_view = make_mock_callback(chat_id=1001, user=owner_tg, data=f"admin:u_view:{user_id}", bot=mock_bot)
    object.__setattr__(cb_u_view.message, "edit_text", AsyncMock())
    object.__setattr__(cb_u_view, "answer", AsyncMock())
    await router.propagate_event("callback_query", cb_u_view, db=db, bot=mock_bot)

    view_text = cb_u_view.message.edit_text.call_args.args[0]
    assert "Товаров: 0 (макс." in view_text, f"Expected 'Товаров: 0' in view text: {view_text}"


@pytest.mark.asyncio
async def test_admin_user_view_v2_active_subscriptions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db, user_id, router, mock_bot, owner_tg = await _setup_test_env(tmp_path, monkeypatch)

    conn = await db.connect()
    await conn.execute("INSERT INTO queries (id, canonical_url) VALUES ('q1', 'https://avito.ru/q1')")
    await conn.execute(
        "INSERT INTO subscriptions (id, user_id, query_id, max_price, name, enabled, version) "
        "VALUES ('sub_1', ?, 'q1', 50000, 'Sony A6000', 1, 1)",
        (user_id,),
    )
    await conn.commit()

    cb_users = make_mock_callback(chat_id=1001, user=owner_tg, data="admin:users", bot=mock_bot)
    object.__setattr__(cb_users.message, "edit_text", AsyncMock())
    object.__setattr__(cb_users, "answer", AsyncMock())
    await router.propagate_event("callback_query", cb_users, db=db, bot=mock_bot)

    users_kb = cb_users.message.edit_text.call_args.kwargs["reply_markup"]
    btn_texts = [btn.text for row in users_kb.inline_keyboard for btn in row]
    assert any("1 тов." in t and "Егор" in t for t in btn_texts), f"Expected '1 тов.' in buttons: {btn_texts}"

    cb_u_view = make_mock_callback(chat_id=1001, user=owner_tg, data=f"admin:u_view:{user_id}", bot=mock_bot)
    object.__setattr__(cb_u_view.message, "edit_text", AsyncMock())
    object.__setattr__(cb_u_view, "answer", AsyncMock())
    await router.propagate_event("callback_query", cb_u_view, db=db, bot=mock_bot)

    view_text = cb_u_view.message.edit_text.call_args.args[0]
    assert "Товаров: 1 (макс." in view_text, f"Expected 'Товаров: 1' in view text: {view_text}"


@pytest.mark.asyncio
async def test_admin_stats_v2_subscriptions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db, user_id, router, mock_bot, owner_tg = await _setup_test_env(tmp_path, monkeypatch)

    # Subscriptions table is empty, but store has 1 legacy yaml profile (p1)
    cb_stats = make_mock_callback(chat_id=1001, user=owner_tg, data="admin:stats", bot=mock_bot)
    object.__setattr__(cb_stats.message, "edit_text", AsyncMock())
    object.__setattr__(cb_stats, "answer", AsyncMock())
    await router.propagate_event("callback_query", cb_stats, db=db, bot=mock_bot)

    stats_text = cb_stats.message.edit_text.call_args.args[0]
    assert "• Live: 0 | Shadow: 0 | Пауза: 0 | Всего: 0" in stats_text, f"Unexpected stats text: {stats_text}"


@pytest.mark.asyncio
async def test_admin_user_v2_pause_resume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db, user_id, router, mock_bot, owner_tg = await _setup_test_env(tmp_path, monkeypatch)

    conn = await db.connect()
    await conn.execute("INSERT INTO queries (id, canonical_url) VALUES ('q1', 'https://avito.ru/q1')")
    await conn.execute(
        "INSERT INTO subscriptions (id, user_id, query_id, max_price, name, enabled, version) "
        "VALUES ('sub_1', ?, 'q1', 50000, 'Sony A6000', 1, 1)",
        (user_id,),
    )
    await conn.commit()

    cb_pause = make_mock_callback(chat_id=1001, user=owner_tg, data=f"admin:u_pause:{user_id}", bot=mock_bot)
    object.__setattr__(cb_pause.message, "edit_text", AsyncMock())
    object.__setattr__(cb_pause, "answer", AsyncMock())
    await router.propagate_event("callback_query", cb_pause, db=db, bot=mock_bot)

    cur = await conn.execute("SELECT enabled FROM subscriptions WHERE id = 'sub_1'")
    assert (await cur.fetchone())[0] == 0

    cb_resume = make_mock_callback(chat_id=1001, user=owner_tg, data=f"admin:u_resume:{user_id}", bot=mock_bot)
    object.__setattr__(cb_resume.message, "edit_text", AsyncMock())
    object.__setattr__(cb_resume, "answer", AsyncMock())
    await router.propagate_event("callback_query", cb_resume, db=db, bot=mock_bot)

    cur = await conn.execute("SELECT enabled FROM subscriptions WHERE id = 'sub_1'")
    assert (await cur.fetchone())[0] == 1



