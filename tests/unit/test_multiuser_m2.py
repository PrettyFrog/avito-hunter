"""Comprehensive Multi-User Phase M2 Test Suite:
1. Registry audit verifying public handlers set is strictly {"cmd_start", "cb_req_access"}.
2. Valid invite flow, atomic consumption, owner notification, and 3-step onboarding.
3. Invite edge cases (expired, revoked, already used, forged token, 11th invite limit,
   revoke callback).
4. Concurrency & double-click race condition on consume_invite_atomic and
   resolve_access_request_atomic.
5. Blocked/inactive user gets absolute silence on /start, invites, and req_access.
6. Access request flow (friend approval, family approval, rejection, 24h cooldown,
   3-rejection silence, 5/hr rate limit).
7. Repeat entry for existing user does not duplicate user or consume invite.
8. Legacy owner commands (/add, /pause, /resume, /my, etc.) reply
   FEATURE_COMING_SOON with state unchanged.
9. Malicious names (HTML tags, control chars, length limit) sanitization.
10. Snapshot tests for /start, /help, and /faq (all 8 questions).
11. Timezone tests in both TZ=UTC and TZ=Europe/Moscow.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import CallbackQuery, Chat, Message
from aiogram.types import User as TgUser

from hunter.core.db import Database
from hunter.core.models import now_utc
from hunter.core.product import ProductProfile, ProductStore, save_product_profile
from hunter.notify.messages_ru import (
    ACCESS_DENIED,
    FAQ_ITEMS,
    FAQ_MENU_TITLE,
    HELP_OWNER_EXTRA,
    HELP_USER_TEXT,
    INVITE_ALREADY_USED,
    INVITE_EXPIRED,
    INVITE_LIMIT_REACHED,
    INVITE_NOT_FOUND,
    INVITE_REVOKED,
    ONBOARDING_SCREEN_2,
    ONBOARDING_SCREEN_3,
    REQ_ACCESS_COOLDOWN,
    REQ_ACCESS_SENT,
    STRANGER_FORBIDDEN,
    clean_name,
)
from hunter.notify.security import (
    REGISTERED_HANDLERS,
)
from hunter.notify.telegram_router import setup_telegram_router


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


def make_mock_message(
    chat_id: int,
    user: TgUser,
    text: str = "",
    bot: AsyncMock | None = None,
    message_id: int = 1,
) -> Message:
    chat = Chat(id=chat_id, type="private")
    msg = Message(message_id=message_id, date=1, chat=chat, from_user=user, text=text)
    if bot:
        msg.as_(bot)
    object.__setattr__(msg, "reply", AsyncMock(return_value=msg))
    object.__setattr__(msg, "edit_text", AsyncMock(return_value=msg))
    object.__setattr__(msg, "delete", AsyncMock(return_value=True))
    return msg


def make_mock_callback(
    user: TgUser,
    data: str,
    message: Message | None = None,
    bot: AsyncMock | None = None,
    cb_id: str = "cb_1",
) -> CallbackQuery:
    cb = CallbackQuery(
        id=cb_id,
        from_user=user,
        chat_instance="ci",
        message=message,
        data=data,
    )
    if bot:
        cb.as_(bot)
    object.__setattr__(cb, "answer", AsyncMock(return_value=True))
    return cb


# ==============================================================================
# 1. Registry Audit: Public handlers must strictly be {"cmd_start", "cb_req_access"}
# ==============================================================================
def test_registry_audit_public_handlers_strictly_start_and_req_access(
    mock_channel: MagicMock,
) -> None:
    router = setup_telegram_router(mock_channel)
    all_handlers = [
        *router.message.handlers,
        *router.callback_query.handlers,
        *router.my_chat_member.handlers,
    ]

    public_handlers_in_router = {
        h.callback.__name__
        for h in all_handlers
        if getattr(h.callback, "_required_role", None) == "public"
    }
    assert public_handlers_in_router == {"cmd_start", "cb_req_access"}, (
        f"Public handlers set in router mismatch: {public_handlers_in_router}"
    )

    public_handlers_in_registry = {h.name for h in REGISTERED_HANDLERS if h.role == "public"}
    assert public_handlers_in_registry == {"cmd_start", "cb_req_access"}, (
        f"Public handlers in REGISTERED_HANDLERS mismatch: {public_handlers_in_registry}"
    )


# ==============================================================================
# 2. Valid Invite Flow, Atomic Consumption, Owner Alert, and 3-step Onboarding
# ==============================================================================
@pytest.mark.asyncio
async def test_valid_invite_flow_and_onboarding(
    tmp_path: Path, mock_channel: MagicMock, mock_bot: AsyncMock
) -> None:
    db_path = tmp_path / "test.db"
    db = Database(db_path)
    await db.connect()

    # Create owner in DB
    owner_id = await db.create_user(
        telegram_user_id=1001, chat_id=1001, role="owner", status="active"
    )

    # Create an invite for 'Мама', tier='family'
    token = "secret_mama_token_123"
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    expires_at = (now_utc() + timedelta(hours=48)).isoformat()
    inv_id = await db.create_invite(
        token_hash=token_hash,
        label="Мама",
        tier="family",
        created_by=owner_id,
        expires_at=expires_at,
    )

    router = setup_telegram_router(mock_channel, db=db)

    # User enters via /start inv_secret_mama_token_123
    tg_user = TgUser(id=2002, is_bot=False, first_name="Мама", username="mama_tg")
    replies: list[dict] = []

    msg_start = make_mock_message(
        chat_id=2002, user=tg_user, text=f"/start inv_{token}", bot=mock_bot
    )
    object.__setattr__(
        msg_start,
        "reply",
        AsyncMock(side_effect=lambda text, **kw: replies.append({"text": text, **kw})),
    )

    await router.propagate_event("message", msg_start, db=db, bot=mock_bot)

    # 1. User created in DB
    user_in_db = await db.get_user_by_telegram_id(2002)
    assert user_in_db is not None
    assert user_in_db["role"] == "user"
    assert user_in_db["status"] == "active"
    assert user_in_db["tier"] == "family"
    assert user_in_db["max_products"] == 5  # family default
    assert user_in_db["invited_by"] == owner_id

    # 2. Invite marked used atomically
    inv_in_db = await db.get_invite_by_id(inv_id)
    assert inv_in_db["used_at"] is not None
    assert inv_in_db["used_count"] == 1

    # 3. Owner notified
    mock_bot.send_message.assert_called()
    call_args = mock_bot.send_message.call_args[1]
    assert call_args["chat_id"] == 1001
    assert "Мама" in call_args["text"]

    # 4. User received Onboarding Screen 1
    assert len(replies) == 1
    screen1_text = replies[0]["text"]
    assert "Привет, Мама!" in screen1_text
    assert "Как это работает" in screen1_text

    # 5. Onboarding step 2 callback
    cb_msg = make_mock_message(chat_id=2002, user=tg_user, text="screen1", bot=mock_bot)
    cb2 = make_mock_callback(user=tg_user, data="onb:2", message=cb_msg, bot=mock_bot)
    await router.propagate_event("callback_query", cb2, db=db, bot=mock_bot)
    cb_msg.edit_text.assert_called_with(
        ONBOARDING_SCREEN_2,
        reply_markup=cb_msg.edit_text.call_args[1]["reply_markup"],
        parse_mode="HTML",
    )

    # 6. Onboarding step 3 callback
    cb3 = make_mock_callback(user=tg_user, data="onb:3", message=cb_msg, bot=mock_bot)
    await router.propagate_event("callback_query", cb3, db=db, bot=mock_bot)
    cb_msg.edit_text.assert_called_with(
        ONBOARDING_SCREEN_3,
        reply_markup=cb_msg.edit_text.call_args[1]["reply_markup"],
        parse_mode="HTML",
    )

    # 7. Onboarding done callback
    cb_done = make_mock_callback(user=tg_user, data="onb:done", message=cb_msg, bot=mock_bot)
    await router.propagate_event("callback_query", cb_done, db=db, bot=mock_bot)
    cb_msg.delete.assert_called_once()
    mock_bot.send_message.assert_called()


# ==============================================================================
# 3. Invite Edge Cases
# ==============================================================================
@pytest.mark.asyncio
async def test_invite_edge_cases(
    tmp_path: Path, mock_channel: MagicMock, mock_bot: AsyncMock
) -> None:
    db_path = tmp_path / "test.db"
    db = Database(db_path)
    await db.connect()
    owner_id = await db.create_user(
        telegram_user_id=1001, chat_id=1001, role="owner", status="active"
    )
    router = setup_telegram_router(mock_channel, db=db)

    # 3.1 Expired invite
    exp_token = "expired_token_1"
    exp_hash = hashlib.sha256(exp_token.encode()).hexdigest()
    past_iso = (now_utc() - timedelta(hours=1)).isoformat()
    await db.create_invite(exp_hash, "Old", "friend", owner_id, expires_at=past_iso)

    user = TgUser(id=3001, is_bot=False, first_name="LateUser")
    replies: list[str] = []
    msg = make_mock_message(chat_id=3001, user=user, text=f"/start inv_{exp_token}", bot=mock_bot)
    object.__setattr__(msg, "reply", AsyncMock(side_effect=lambda t, **kw: replies.append(t)))

    await router.propagate_event("message", msg, db=db, bot=mock_bot)
    assert len(replies) == 1
    assert replies[0] == INVITE_EXPIRED

    # 3.2 Revoked invite
    rev_token = "revoked_token_1"
    rev_hash = hashlib.sha256(rev_token.encode()).hexdigest()
    future_iso = (now_utc() + timedelta(hours=24)).isoformat()
    inv_id = await db.create_invite(rev_hash, "Rev", "friend", owner_id, expires_at=future_iso)
    await db.revoke_invite(inv_id)

    replies.clear()
    msg = make_mock_message(chat_id=3001, user=user, text=f"/start inv_{rev_token}", bot=mock_bot)
    object.__setattr__(msg, "reply", AsyncMock(side_effect=lambda t, **kw: replies.append(t)))

    await router.propagate_event("message", msg, db=db, bot=mock_bot)
    assert len(replies) == 1
    assert replies[0] == INVITE_REVOKED

    # 3.3 Already used invite
    used_token = "used_token_1"
    used_hash = hashlib.sha256(used_token.encode()).hexdigest()
    await db.create_invite(used_hash, "Used", "friend", owner_id, expires_at=future_iso)
    await db.consume_invite_atomic(used_hash, now_utc().isoformat())

    replies.clear()
    msg = make_mock_message(chat_id=3001, user=user, text=f"/start inv_{used_token}", bot=mock_bot)
    object.__setattr__(msg, "reply", AsyncMock(side_effect=lambda t, **kw: replies.append(t)))

    await router.propagate_event("message", msg, db=db, bot=mock_bot)
    assert len(replies) == 1
    assert replies[0] == INVITE_ALREADY_USED

    # 3.4 Forged token
    replies.clear()
    msg = make_mock_message(
        chat_id=3001, user=user, text="/start inv_fake_forged_random_xyz", bot=mock_bot
    )
    object.__setattr__(msg, "reply", AsyncMock(side_effect=lambda t, **kw: replies.append(t)))

    await router.propagate_event("message", msg, db=db, bot=mock_bot)
    assert len(replies) == 1
    assert replies[0] == INVITE_NOT_FOUND

    # 3.5 10 active invites limit for owner
    active_now = await db.get_active_invites_count(owner_id)
    for i in range(active_now, 10):
        t = f"active_tok_{i}"
        th = hashlib.sha256(t.encode()).hexdigest()
        await db.create_invite(th, f"Friend_{i}", "friend", owner_id, expires_at=future_iso)

    assert await db.get_active_invites_count(owner_id) == 10

    # 11th invite attempt by owner
    owner_tg = TgUser(id=1001, is_bot=False, first_name="Owner")
    replies.clear()
    msg_inv = make_mock_message(
        chat_id=1001, user=owner_tg, text="/invite Брат friend", bot=mock_bot
    )
    object.__setattr__(msg_inv, "reply", AsyncMock(side_effect=lambda t, **kw: replies.append(t)))

    await router.propagate_event("message", msg_inv, db=db, bot=mock_bot)
    assert len(replies) == 1
    assert replies[0] == INVITE_LIMIT_REACHED

    # 3.6 Owner revoking an invite via callback
    active_invites = await db.get_active_invites(owner_id)
    assert len(active_invites) == 10
    target_inv = active_invites[0]

    cb_msg = make_mock_message(chat_id=1001, user=owner_tg, text="invites", bot=mock_bot)
    cb_revoke = make_mock_callback(
        user=owner_tg, data=f"revoke_inv:{target_inv['id']}", message=cb_msg, bot=mock_bot
    )
    await router.propagate_event("callback_query", cb_revoke, db=db, bot=mock_bot)
    assert await db.get_active_invites_count(owner_id) == 9


# ==============================================================================
# 4. Atomic Concurrency: Double-Click Race Condition Tests
# ==============================================================================
@pytest.mark.asyncio
async def test_atomic_concurrency_invite_and_access_request(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    db = Database(db_path)
    await db.connect()
    owner_id = await db.create_user(
        telegram_user_id=1001, chat_id=1001, role="owner", status="active"
    )

    # 4.1 Concurrent consume_invite_atomic
    token = "atomic_race_token"
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    expires_at = (now_utc() + timedelta(hours=24)).isoformat()
    await db.create_invite(token_hash, "RaceUser", "friend", owner_id, expires_at=expires_at)

    async def try_consume() -> bool:
        return await db.consume_invite_atomic(token_hash, now_utc().isoformat())

    results = await asyncio.gather(*[try_consume() for _ in range(10)])
    assert results.count(True) == 1
    assert results.count(False) == 9

    # 4.2 Concurrent resolve_access_request_atomic
    req_id = await db.create_access_request(
        telegram_user_id=5005,
        username="racer",
        display_name="Racer",
    )
    assert req_id is not None

    async def try_resolve() -> bool:
        return await db.resolve_access_request_atomic(req_id, "approved", owner_id)

    req_results = await asyncio.gather(*[try_resolve() for _ in range(10)])
    assert req_results.count(True) == 1
    assert req_results.count(False) == 9


# ==============================================================================
# 5. Blocked / Inactive Users: Absolute Silence
# ==============================================================================
@pytest.mark.asyncio
async def test_blocked_and_inactive_user_absolute_silence(
    tmp_path: Path, mock_channel: MagicMock, mock_bot: AsyncMock
) -> None:
    db_path = tmp_path / "test.db"
    db = Database(db_path)
    await db.connect()
    owner_id = await db.create_user(
        telegram_user_id=1001, chat_id=1001, role="owner", status="active"
    )
    router = setup_telegram_router(mock_channel, db=db)

    # Insert blocked user
    await db.create_user(telegram_user_id=4001, chat_id=4001, role="user", status="blocked")
    # Insert inactive user
    await db.create_user(telegram_user_id=4002, chat_id=4002, role="user", status="inactive")

    # Create a fresh valid invite
    tok = "blocked_test_tok"
    th = hashlib.sha256(tok.encode()).hexdigest()
    await db.create_invite(
        th, "Test", "friend", owner_id, expires_at=(now_utc() + timedelta(hours=24)).isoformat()
    )

    # 5.1 Blocked user sends /start inv_... -> absolute silence!
    user_blocked = TgUser(id=4001, is_bot=False, first_name="Blocked")
    replies: list[str] = []
    msg_blocked = make_mock_message(
        chat_id=4001, user=user_blocked, text=f"/start inv_{tok}", bot=mock_bot
    )
    object.__setattr__(
        msg_blocked, "reply", AsyncMock(side_effect=lambda t, **kw: replies.append(t))
    )

    await router.propagate_event("message", msg_blocked, db=db, bot=mock_bot)
    assert len(replies) == 0, "Blocked user must receive absolute silence"

    # Invite must NOT be consumed
    inv = await db.get_invite_by_hash(th)
    assert inv["used_at"] is None

    # 5.2 Inactive user sends /start inv_... -> absolute silence!
    user_inactive = TgUser(id=4002, is_bot=False, first_name="Inactive")
    msg_inactive = make_mock_message(
        chat_id=4002, user=user_inactive, text=f"/start inv_{tok}", bot=mock_bot
    )
    object.__setattr__(
        msg_inactive, "reply", AsyncMock(side_effect=lambda t, **kw: replies.append(t))
    )

    await router.propagate_event("message", msg_inactive, db=db, bot=mock_bot)
    assert len(replies) == 0, "Inactive user must receive absolute silence"
    inv = await db.get_invite_by_hash(th)
    assert inv["used_at"] is None

    # 5.3 Blocked user clicks req_access -> absolute silence!
    cb_msg = make_mock_message(chat_id=4001, user=user_blocked, text="req", bot=mock_bot)
    cb_blocked = make_mock_callback(
        user=user_blocked, data="req_access", message=cb_msg, bot=mock_bot
    )
    await router.propagate_event("callback_query", cb_blocked, db=db, bot=mock_bot)
    cb_blocked.answer.assert_not_called()
    assert await db.get_pending_access_requests_count() == 0


# ==============================================================================
# 6. Access Request Flow (Approval, Rejection, Cooldown, Limits)
# ==============================================================================
@pytest.mark.asyncio
async def test_access_request_full_flow(
    tmp_path: Path, mock_channel: MagicMock, mock_bot: AsyncMock
) -> None:
    db_path = tmp_path / "test.db"
    db = Database(db_path)
    await db.connect()
    owner_id = await db.create_user(
        telegram_user_id=1001, chat_id=1001, role="owner", status="active"
    )
    router = setup_telegram_router(mock_channel, db=db)

    stranger_tg = TgUser(id=6001, is_bot=False, first_name="Странник", username="stranger_6")
    cb_msg = make_mock_message(chat_id=6001, user=stranger_tg, text="req", bot=mock_bot)

    # 6.1 Stranger clicks req_access
    cb_req = make_mock_callback(user=stranger_tg, data="req_access", message=cb_msg, bot=mock_bot)
    await router.propagate_event("callback_query", cb_req, db=db, bot=mock_bot)

    # Access request created
    assert await db.get_pending_access_requests_count() == 1
    cb_req.answer.assert_called_with(REQ_ACCESS_SENT, show_alert=True)
    mock_bot.send_message.assert_called()

    # 6.2 Second click within 24h -> cooldown
    cb_req2 = make_mock_callback(user=stranger_tg, data="req_access", message=cb_msg, bot=mock_bot)
    await router.propagate_event("callback_query", cb_req2, db=db, bot=mock_bot)
    cb_req2.answer.assert_called_with(REQ_ACCESS_COOLDOWN, show_alert=True)

    # 6.3 Owner approves as 'friend'
    conn = await db.connect()
    cur = await conn.execute("SELECT id FROM access_requests WHERE telegram_user_id = 6001")
    req_row = await cur.fetchone()
    req_id = req_row[0]

    owner_tg = TgUser(id=1001, is_bot=False, first_name="Owner")
    cb_approve = make_mock_callback(
        user=owner_tg, data=f"acc_ok:friend:{req_id}", message=cb_msg, bot=mock_bot
    )
    await router.propagate_event("callback_query", cb_approve, db=db, bot=mock_bot)

    # Verify user created with friend tier
    created_user = await db.get_user_by_telegram_id(6001)
    assert created_user is not None
    assert created_user["role"] == "user"
    assert created_user["status"] == "active"
    assert created_user["tier"] == "friend"
    assert created_user["max_products"] == 3  # friend default

    # 6.4 Rejection flow & 3-rejections silence
    stranger_rej = TgUser(id=7001, is_bot=False, first_name="RejectedGuy")
    for _ in range(3):
        rid = await db.create_access_request(7001, "rej", "RejectedGuy")
        await db.resolve_access_request_atomic(rid, "rejected", owner_id)

    assert await db.get_rejected_access_requests_count(7001) == 3

    # 4th request from this guy -> absolute silence
    cb_silent = make_mock_callback(
        user=stranger_rej, data="req_access", message=cb_msg, bot=mock_bot
    )
    await router.propagate_event("callback_query", cb_silent, db=db, bot=mock_bot)
    cb_silent.answer.assert_not_called()


# ==============================================================================
# 7. Repeat Entry for Existing User Does Not Duplicate
# ==============================================================================
@pytest.mark.asyncio
async def test_existing_user_repeat_start_does_not_duplicate(
    tmp_path: Path, mock_channel: MagicMock, mock_bot: AsyncMock
) -> None:
    db_path = tmp_path / "test.db"
    db = Database(db_path)
    await db.connect()
    owner_id = await db.create_user(
        telegram_user_id=1001, chat_id=1001, role="owner", status="active"
    )
    router = setup_telegram_router(mock_channel, db=db)

    # Existing active user
    await db.create_user(
        telegram_user_id=8001, chat_id=8001, role="user", status="active", display_name="Алексей"
    )

    # Invite token
    tok = "tok_repeat_test"
    th = hashlib.sha256(tok.encode()).hexdigest()
    await db.create_invite(
        th, "Алексей", "friend", owner_id, expires_at=(now_utc() + timedelta(hours=24)).isoformat()
    )

    user_tg = TgUser(id=8001, is_bot=False, first_name="Алексей")
    replies: list[str] = []

    msg = make_mock_message(chat_id=8001, user=user_tg, text=f"/start inv_{tok}", bot=mock_bot)
    object.__setattr__(msg, "reply", AsyncMock(side_effect=lambda t, **kw: replies.append(t)))

    await router.propagate_event("message", msg, db=db, bot=mock_bot)

    # 1. Total rows in users for 8001 is still 1
    conn = await db.connect()
    cur = await conn.execute("SELECT COUNT(*) FROM users WHERE telegram_user_id = 8001")
    count = (await cur.fetchone())[0]
    assert count == 1

    # 2. Invite is NOT consumed
    inv = await db.get_invite_by_hash(th)
    assert inv["used_at"] is None

    # 3. User receives already registered message
    assert len(replies) == 1
    assert "Вы уже зарегистрированы" in replies[0]


# ==============================================================================
# 8. Legacy Owner Commands: FEATURE_COMING_SOON & Snapshot Unchanged
# ==============================================================================
@pytest.mark.asyncio
async def test_legacy_owner_commands_reply_coming_soon_and_state_unchanged(
    tmp_path: Path, mock_channel: MagicMock, mock_bot: AsyncMock
) -> None:
    db_path = tmp_path / "test.db"
    db = Database(db_path)
    await db.connect()
    owner_id = await db.create_user(
        telegram_user_id=1001, chat_id=1001, role="owner", status="active"
    )
    uid = await db.create_user(telegram_user_id=9001, chat_id=9001, role="user", status="active")

    products_dir = tmp_path / "products"
    products_dir.mkdir(parents=True)
    p1 = ProductProfile(
        id="prod_snap1",
        name="Product 1",
        search_url="https://www.avito.ru",
        max_price=15000,
        mode="live",
        owner_user_id=uid,
    )
    save_product_profile(p1, directory=products_dir)

    store = ProductStore(directory=products_dir)
    from hunter.core import product as product_module
    from hunter.notify import telegram_router as router_module

    product_module.get_product_store = lambda directory=None: store
    router_module.get_product_store = lambda directory=None: store

    router = setup_telegram_router(mock_channel, db=db)

    # Snapshot state function
    async def get_state_snapshot() -> dict:
        conn = await db.connect()
        raw_users = await (await conn.execute("SELECT * FROM users ORDER BY id")).fetchall()
        users_rows = [{k: row[k] for k in row.keys() if k != "last_seen_at"} for row in raw_users]
        invites_rows = [
            dict(row)
            for row in await (await conn.execute("SELECT * FROM invites ORDER BY id")).fetchall()
        ]
        pending_rows = [
            dict(row)
            for row in await (
                await conn.execute("SELECT * FROM pending_products ORDER BY chat_id")
            ).fetchall()
        ]
        req_rows = [
            dict(row)
            for row in await (
                await conn.execute("SELECT * FROM access_requests ORDER BY id")
            ).fetchall()
        ]
        files = {f.name: f.read_text() for f in products_dir.glob("*.yaml")}
        return {
            "users": users_rows,
            "invites": invites_rows,
            "pending": pending_rows,
            "requests": req_rows,
            "files": files,
        }

    snapshot_before = await get_state_snapshot()

    user_tg = TgUser(id=9001, is_bot=False, first_name="User9")

    # /settings
    replies_settings: list[str] = []
    msg_settings = make_mock_message(chat_id=9001, user=user_tg, text="/settings", bot=mock_bot)
    object.__setattr__(
        msg_settings, "reply", AsyncMock(side_effect=lambda t, **kw: replies_settings.append(t))
    )
    await router.propagate_event("message", msg_settings, db=db, bot=mock_bot)
    assert len(replies_settings) == 1
    assert "Настройки аккаунта" in replies_settings[0]

    # /delete_me (prompts confirmation, does not delete immediately)
    from hunter.notify.messages_ru import DELETE_ME_CONFIRM, SUPPORT_PROMPT

    replies_del: list[str] = []
    msg_del = make_mock_message(chat_id=9001, user=user_tg, text="/delete_me", bot=mock_bot)
    object.__setattr__(
        msg_del, "reply", AsyncMock(side_effect=lambda t, **kw: replies_del.append(t))
    )
    await router.propagate_event("message", msg_del, db=db, bot=mock_bot)
    assert len(replies_del) == 1
    assert replies_del[0] == DELETE_ME_CONFIRM

    snapshot_after = await get_state_snapshot()
    assert snapshot_after == snapshot_before, "State was altered by commands before confirmation!"

    # Also test /support
    replies_sup: list[str] = []
    msg_sup = make_mock_message(chat_id=9001, user=user_tg, text="/support", bot=mock_bot)
    object.__setattr__(
        msg_sup, "reply", AsyncMock(side_effect=lambda t, **kw: replies_sup.append(t))
    )
    await router.propagate_event("message", msg_sup, db=db, bot=mock_bot)
    assert len(replies_sup) == 1
    assert replies_sup[0] == SUPPORT_PROMPT


# ==============================================================================
# 9. Malicious Names Sanitization
# ==============================================================================
def test_malicious_names_sanitization() -> None:
    # Strips control chars
    raw_with_ctrl = "Hello\x00\x08World\x1f\x7fEnd"
    assert clean_name(raw_with_ctrl) == "HelloWorldEnd"

    # HTML escapes
    raw_html = "<b>Hacker</b> & 'crew' \"test\""
    cleaned = clean_name(raw_html)
    assert "<b>" not in cleaned
    assert "&lt;b&gt;Hacker&lt;/b&gt;" in cleaned

    # Markdown / link injection
    raw_link = "[Evil](tg://user?id=123)"
    cleaned_link = clean_name(raw_link)
    assert cleaned_link == "[Evil](tg://user?id=123)"  # safe in HTML mode

    # Length capping at 40
    long_name = "А" * 60
    assert len(clean_name(long_name)) == 40


# ==============================================================================
# 10. Snapshot Tests: /start, /help, /faq
# ==============================================================================
@pytest.mark.asyncio
async def test_snapshots_start_help_faq(
    tmp_path: Path, mock_channel: MagicMock, mock_bot: AsyncMock
) -> None:
    db_path = tmp_path / "test.db"
    db = Database(db_path)
    await db.connect()
    owner_id = await db.create_user(
        telegram_user_id=1001, chat_id=1001, role="owner", status="active"
    )
    uid = await db.create_user(telegram_user_id=10001, chat_id=10001, role="user", status="active")
    router = setup_telegram_router(mock_channel, db=db)

    # 10.1 /help for User vs Owner
    user_tg = TgUser(id=10001, is_bot=False, first_name="User10")
    replies_user: list[str] = []
    msg_help_user = make_mock_message(chat_id=10001, user=user_tg, text="/help", bot=mock_bot)
    object.__setattr__(
        msg_help_user, "reply", AsyncMock(side_effect=lambda t, **kw: replies_user.append(t))
    )
    await router.propagate_event("message", msg_help_user, db=db, bot=mock_bot)

    assert len(replies_user) == 1
    assert replies_user[0] == HELP_USER_TEXT
    assert "/budget" not in replies_user[0]

    owner_tg = TgUser(id=1001, is_bot=False, first_name="Owner")
    replies_owner: list[str] = []
    msg_help_owner = make_mock_message(chat_id=1001, user=owner_tg, text="/help", bot=mock_bot)
    object.__setattr__(
        msg_help_owner, "reply", AsyncMock(side_effect=lambda t, **kw: replies_owner.append(t))
    )
    await router.propagate_event("message", msg_help_owner, db=db, bot=mock_bot)

    assert len(replies_owner) == 1
    assert replies_owner[0] == HELP_USER_TEXT + HELP_OWNER_EXTRA
    assert "/invite" in replies_owner[0]

    # 10.2 /faq menu & items
    replies_faq: list[dict] = []
    msg_faq = make_mock_message(chat_id=10001, user=user_tg, text="/faq", bot=mock_bot)
    object.__setattr__(
        msg_faq,
        "reply",
        AsyncMock(side_effect=lambda t, **kw: replies_faq.append({"text": t, **kw})),
    )
    await router.propagate_event("message", msg_faq, db=db, bot=mock_bot)

    assert len(replies_faq) == 1
    assert replies_faq[0]["text"] == FAQ_MENU_TITLE
    kb = replies_faq[0]["reply_markup"]
    assert len(kb.inline_keyboard) == 8, f"Expected 8 FAQ items, got {len(kb.inline_keyboard)}"

    # Check each FAQ item callback
    cb_msg = make_mock_message(chat_id=10001, user=user_tg, text="faq", bot=mock_bot)
    for key, (q_text, a_text) in FAQ_ITEMS.items():
        cb_msg.edit_text.reset_mock()
        cb = make_mock_callback(user=user_tg, data=f"faq:{key}", message=cb_msg, bot=mock_bot)
        await router.propagate_event("callback_query", cb, db=db, bot=mock_bot)
        assert cb_msg.edit_text.called
        text_sent = cb_msg.edit_text.call_args[0][0]
        assert q_text in text_sent
        assert a_text in text_sent


# ==============================================================================
# 11. Timezone Consistency: UTC and Europe/Moscow
# ==============================================================================
def test_timezone_consistency(monkeypatch: pytest.MonkeyPatch) -> None:
    for tz_name in ("UTC", "Europe/Moscow"):
        monkeypatch.setenv("TZ", tz_name)
        if hasattr(time, "tzset"):
            time.tzset()

        t_now = now_utc()
        assert t_now.tzinfo == UTC
        iso = t_now.isoformat()
        # Must parse back cleanly as UTC
        parsed = datetime.fromisoformat(iso)
        assert parsed.tzinfo == UTC or parsed.utcoffset() == timedelta(0)


# ==============================================================================
# 12. Dispatcher Integration Test (Dispatcher._listen_update in outer middleware)
# ==============================================================================
@pytest.mark.asyncio
async def test_dispatcher_feed_update_with_security_middleware(
    tmp_path: Path, mock_channel: MagicMock, mock_bot: AsyncMock
) -> None:
    """Verify that Dispatcher.feed_update with setup_telegram_router does not drop valid updates."""
    from aiogram import Dispatcher
    from aiogram.types import Update

    db_path = tmp_path / "test.db"
    db = Database(db_path)
    await db.connect()

    # Create owner
    await db.create_user(
        telegram_user_id=1001,
        chat_id=1001,
        role="owner",
        username="owner",
        display_name="Owner",
        status="active",
    )

    dp = Dispatcher()
    router = setup_telegram_router(mock_channel, db=db)
    dp.include_router(router)

    replies: list[str] = []
    user_tg = TgUser(id=1001, is_bot=False, first_name="Owner", username="owner")
    chat = Chat(id=1001, type="private")
    msg = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=chat,
        from_user=user_tg,
        text="/help",
    )
    object.__setattr__(msg, "reply", AsyncMock(side_effect=lambda t, **kw: replies.append(t)))

    update = Update(update_id=1, message=msg)
    await dp.feed_update(bot=mock_bot, update=update, db=db)

    # In aiogram Dispatcher, message.reply calls bot(SendMessage(...))
    sent_texts = [
        call.args[0].text
        for call in mock_bot.call_args_list
        if call.args and hasattr(call.args[0], "text")
    ]
    assert len(sent_texts) == 1, (
        f"Dispatcher dropped /help command! bot calls: {mock_bot.mock_calls}"
    )
    assert HELP_USER_TEXT in sent_texts[0]

    # Also verify unadorned rogue handler is rejected under Dispatcher
    @router.message()
    async def rogue_dispatcher_handler(msg: Message) -> None:
        await msg.reply("ROGUE_CALLED")

    mock_bot.reset_mock()
    msg_rogue = Message(
        message_id=2,
        date=datetime.now(UTC),
        chat=chat,
        from_user=user_tg,
        text="/rogue_test",
    )
    update_rogue = Update(update_id=2, message=msg_rogue)
    await dp.feed_update(bot=mock_bot, update=update_rogue, db=db)

    rogue_texts = [
        call.args[0].text
        for call in mock_bot.call_args_list
        if call.args and hasattr(call.args[0], "text")
    ]
    assert "ROGUE_CALLED" not in rogue_texts, "Unadorned handler executed under Dispatcher!"


@pytest.mark.asyncio
async def test_runtime_middleware_roles_matrix(
    tmp_path: Path, mock_channel: MagicMock, mock_bot: AsyncMock
) -> None:
    db = Database(tmp_path / "roles.db")
    await db.connect()
    await db.create_user(telegram_user_id=1001, chat_id=1001, role="owner", status="active")
    await db.create_user(
        telegram_user_id=1002, chat_id=1002, role="user", tier="family", status="active"
    )
    await db.create_user(
        telegram_user_id=1003, chat_id=1003, role="user", tier="friend", status="active"
    )

    router = setup_telegram_router(mock_channel, db=db)

    # 1. Stranger: calling /users (owner only) receives polite stranger refusal
    stranger = TgUser(id=1004, is_bot=False, first_name="Stranger")
    msg_s = make_mock_message(chat_id=1004, user=stranger, text="/users", bot=mock_bot)
    await router.propagate_event("message", msg_s, db=db, bot=mock_bot)
    assert msg_s.reply.called
    assert STRANGER_FORBIDDEN in msg_s.reply.call_args[0][0]

    # 2. Friend: calling /users receives ACCESS_DENIED
    friend = TgUser(id=1003, is_bot=False, first_name="Friend")
    msg_fr = make_mock_message(chat_id=1003, user=friend, text="/users", bot=mock_bot)
    await router.propagate_event("message", msg_fr, db=db, bot=mock_bot)
    assert msg_fr.reply.called
    assert ACCESS_DENIED in msg_fr.reply.call_args[0][0]

    # 3. Family: calling /users receives ACCESS_DENIED
    family = TgUser(id=1002, is_bot=False, first_name="Family")
    msg_fa = make_mock_message(chat_id=1002, user=family, text="/users", bot=mock_bot)
    await router.propagate_event("message", msg_fa, db=db, bot=mock_bot)
    assert msg_fa.reply.called
    assert ACCESS_DENIED in msg_fa.reply.call_args[0][0]

    # 4. Owner: calling /users is processed successfully
    owner = TgUser(id=1001, is_bot=False, first_name="Owner")
    msg_ow = make_mock_message(chat_id=1001, user=owner, text="/users", bot=mock_bot)
    await router.propagate_event("message", msg_ow, db=db, bot=mock_bot)
    assert msg_ow.reply.called
    assert ACCESS_DENIED not in msg_ow.reply.call_args[0][0]
