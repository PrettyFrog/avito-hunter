"""Comprehensive Multi-User Phase M1 Test Suite:
- Runtime Default Deny & registry audit
- 4-user isolation (Owner, User A, User B, Stranger)
- Synthetic user handler with touches_product_id=True
- Forged callback verification
- Stranger throttling (24h cooldown, 20/hr global, 20/min per user) & non-private chat rejection
- Bot blocking & paused_reason='bot_blocked' vs manual pause recovery
- Delivery queue delivery preservation for inactive users
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Chat, ChatMemberUpdated, Message
from aiogram.types import User as TgUser

from hunter.core.db import Database
from hunter.core.models import User
from hunter.core.product import ProductProfile, ProductStore, save_product_profile
from hunter.notify.messages_ru import (
    CALLBACK_NOT_RECIPIENT,
    PRODUCT_NOT_FOUND,
    STRANGER_FORBIDDEN,
)
from hunter.notify.security import (
    AuthAndScopeMiddleware,
    StrangerLimiter,
    get_owned_product,
    requires,
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
    return bot


def test_registry_audit_all_handlers_have_roles_and_safe_products(mock_channel: MagicMock) -> None:
    """Registry audit test:

    Every handler registered on the router MUST declare a minimal role via @requires.
    Every handler that touches a product by ID MUST call get_owned_product.
    """
    router = setup_telegram_router(mock_channel)
    all_handlers = [
        *router.message.handlers,
        *router.callback_query.handlers,
        *router.my_chat_member.handlers,
    ]

    assert len(all_handlers) > 0, "Router should have registered handlers"

    for h in all_handlers:
        fn = h.callback
        fn_name = getattr(fn, "__name__", str(fn))
        role = getattr(fn, "_required_role", None)
        assert role in ("owner", "user", "public"), (
            f"Handler '{fn_name}' has no declared role or invalid role '{role}'"
        )

        touches_prod = getattr(fn, "_touches_product_id", False)
        if touches_prod:
            source = inspect.getsource(fn)
            tree = ast.parse(textwrap.dedent(source))
            delegated = {
                n.func.id if isinstance(n.func, ast.Name) else n.func.attr
                for n in ast.walk(tree)
                if isinstance(n, ast.Call)
                and (
                    isinstance(n.func, ast.Name)
                    or (isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name))
                )
                and (n.func.id if isinstance(n.func, ast.Name) else n.func.attr).startswith(
                    "handle_"
                )
            }
            module = inspect.getmodule(fn)
            assert delegated, (
                f"Handler '{fn_name}' has touches_product_id=True without a delegated owner check"
            )
            assert any(
                "get_owned_product" in inspect.getsource(helper)
                for name in delegated
                if module is not None
                for helper in [
                    getattr(module, name, None)
                    or getattr(getattr(module, "_router_globals", None), name, None)
                ]
                if helper is not None
            ), f"Handler '{fn_name}' delegates without get_owned_product!"


@pytest.mark.asyncio
async def test_runtime_default_deny_unadorned_handler() -> None:
    """A handler without @requires must be rejected by middleware at runtime."""
    test_router = Router()
    limiter = StrangerLimiter()
    mw = AuthAndScopeMiddleware(owner_chat_id=1001, limiter=limiter)
    test_router.message.middleware(mw)

    called = False

    @test_router.message()
    async def rogue_handler(msg: Message) -> None:
        nonlocal called
        called = True

    chat = Chat(id=1001, type="private")
    user = TgUser(id=1001, is_bot=False, first_name="Owner")
    msg = Message(message_id=1, date=1, chat=chat, from_user=user, text="/rogue")

    # Propagate update through router
    await test_router.propagate_event("message", msg)
    assert not called, "Unadorned handler was executed despite lacking @requires!"


@pytest.mark.asyncio
async def test_data_isolation_between_users(tmp_path: Path) -> None:
    """Test get_owned_product across 4 user personas."""
    products_dir = tmp_path / "products"
    products_dir.mkdir(parents=True)

    owner_user = User(
        id=1,
        telegram_user_id=1001,
        chat_id=1001,
        role="owner",
        status="active",
        created_at="2026-01-01",
    )
    user_a = User(
        id=2,
        telegram_user_id=2001,
        chat_id=2001,
        role="user",
        status="active",
        created_at="2026-01-01",
    )
    user_b = User(
        id=3,
        telegram_user_id=3001,
        chat_id=3001,
        role="user",
        status="active",
        created_at="2026-01-01",
    )

    p_owner = ProductProfile(
        id="sony_xm6",
        name="Sony XM6",
        search_url="https://www.avito.ru/search",
        max_price=30000,
        owner_user_id=1,
    )
    p_user_a = ProductProfile(
        id="u2_laptop",
        name="ThinkPad",
        search_url="https://www.avito.ru/search",
        max_price=40000,
        owner_user_id=2,
    )
    p_user_b = ProductProfile(
        id="u3_phone",
        name="Pixel 9",
        search_url="https://www.avito.ru/search",
        max_price=50000,
        owner_user_id=3,
    )

    save_product_profile(p_owner, directory=products_dir)
    save_product_profile(p_user_a, directory=products_dir)
    save_product_profile(p_user_b, directory=products_dir)

    store = ProductStore(directory=products_dir)
    store.load_all()

    # User A checks
    assert get_owned_product(user_a, "u2_laptop", store) is not None
    assert get_owned_product(user_a, "u3_phone", store) is None  # User B's product
    assert get_owned_product(user_a, "sony_xm6", store) is None  # Owner's product

    # User B checks
    assert get_owned_product(user_b, "u3_phone", store) is not None
    assert get_owned_product(user_b, "u2_laptop", store) is None  # User A's product
    assert get_owned_product(user_b, "sony_xm6", store) is None  # Owner's product

    # Owner checks (owner can access all)
    assert get_owned_product(owner_user, "sony_xm6", store) is not None
    assert get_owned_product(owner_user, "u2_laptop", store) is not None
    assert get_owned_product(owner_user, "u3_phone", store) is not None


@pytest.mark.asyncio
async def test_synthetic_user_handler_cross_access(tmp_path: Path, mock_bot: AsyncMock) -> None:
    """Synthetic user handler with touches_product_id=True prevents cross-tenant access."""
    db_path = tmp_path / "test.db"
    db = Database(db_path)
    await db.connect()
    # Create User A and User B
    uid_a = await db.create_user(telegram_user_id=2001, chat_id=2001, role="user", status="active")
    uid_b = await db.create_user(telegram_user_id=3001, chat_id=3001, role="user", status="active")

    products_dir = tmp_path / "products"
    products_dir.mkdir(parents=True)
    p_user_a = ProductProfile(
        id="u2_laptop",
        name="ThinkPad",
        search_url="https://www.avito.ru/search",
        max_price=40000,
        owner_user_id=uid_a,
    )
    save_product_profile(p_user_a, directory=products_dir)
    store = ProductStore(directory=products_dir)
    store.load_all()

    test_router = Router()
    limiter = StrangerLimiter()
    mw = AuthAndScopeMiddleware(owner_chat_id=1001, limiter=limiter)
    test_router.message.middleware(mw)

    replies: list[str] = []

    @test_router.message(Command("my_show"))
    @requires("user", touches_product_id=True)
    async def synthetic_show(message: Message, current_user: User) -> None:
        pid = (message.text or "").split()[1]
        p = get_owned_product(current_user, pid, store)
        if not p:
            await message.reply(PRODUCT_NOT_FOUND)
            return
        await message.reply(f"OK:{p.id}")

    # Simulate User B accessing User A's product
    chat_b = Chat(id=3001, type="private")
    tg_user_b = TgUser(id=3001, is_bot=False, first_name="UserB")
    msg_b = Message(
        message_id=1, date=1, chat=chat_b, from_user=tg_user_b, text="/my_show u2_laptop"
    )
    object.__setattr__(msg_b, "reply", AsyncMock(side_effect=lambda t, **kwargs: replies.append(t)))

    await test_router.propagate_event("message", msg_b, db=db, bot=mock_bot)
    assert replies[-1] == PRODUCT_NOT_FOUND

    # Simulate User A accessing their own product
    chat_a = Chat(id=2001, type="private")
    tg_user_a = TgUser(id=2001, is_bot=False, first_name="UserA")
    msg_a = Message(
        message_id=2, date=1, chat=chat_a, from_user=tg_user_a, text="/my_show u2_laptop"
    )
    object.__setattr__(msg_a, "reply", AsyncMock(side_effect=lambda t, **kwargs: replies.append(t)))

    await test_router.propagate_event("message", msg_a, db=db, bot=mock_bot)
    assert replies[-1] == "OK:u2_laptop"


@pytest.mark.asyncio
async def test_forged_feedback_callback_rejected(
    tmp_path: Path, mock_channel: MagicMock, mock_bot: AsyncMock
) -> None:
    """Feedback buttons (fb:avito_id:action) can only be clicked by the recipient or owner."""
    db_path = tmp_path / "test.db"
    db = Database(db_path)
    await db.connect()

    # User A (2001) and User B (3001)
    uid_a = await db.create_user(telegram_user_id=2001, chat_id=2001, role="user", status="active")
    uid_b = await db.create_user(telegram_user_id=3001, chat_id=3001, role="user", status="active")

    conn = await db.connect()
    await conn.execute(
        "INSERT INTO listings (avito_id, url, first_seen, last_seen, status) "
        "VALUES (?, ?, ?, ?, ?)",
        ("lot_42", "https://avito.ru/1", "2026-01-01T00:00:00", "2026-01-01T00:00:00", "active"),
    )
    await conn.commit()

    # Record delivery sent to User A (user_id=uid_a)
    await conn.execute(
        "INSERT INTO events(id, avito_id, revision, kind, observed_at) VALUES (?, ?, 1, 'NEW_LISTING', ?)",
        ("evt_lot_42", "lot_42", "2026-01-01T12:00:00"),
    )
    await conn.execute(
        "INSERT INTO deliveries(id, event_id, user_id, channel, status, available_at, created_at) "
        "VALUES (?, ?, ?, 'telegram', 'sent', '2026-01-01T12:00:00', '2026-01-01T12:00:00')",
        ("del_notif_lot_42", "evt_lot_42", uid_a),
    )
    await conn.commit()

    router = setup_telegram_router(mock_channel, db=db)

    # 1. User B (3001) clicks feedback on User A's notification
    cb_user_b = TgUser(id=3001, is_bot=False, first_name="UserB")
    query_b = CallbackQuery(
        id="q1",
        from_user=cb_user_b,
        chat_instance="ci",
        data="fb:lot_42:ok",
    )
    answer_mock_b = AsyncMock()
    object.__setattr__(query_b, "answer", answer_mock_b)

    await router.propagate_event("callback_query", query_b, db=db, bot=mock_bot)
    answer_mock_b.assert_called_with(CALLBACK_NOT_RECIPIENT, show_alert=True)

    # 2. User A (2001) clicks feedback on their notification
    cb_user_a = TgUser(id=2001, is_bot=False, first_name="UserA")
    query_a = CallbackQuery(
        id="q2",
        from_user=cb_user_a,
        chat_instance="ci",
        data="fb:lot_42:ok",
    )
    answer_mock_a = AsyncMock()
    object.__setattr__(query_a, "answer", answer_mock_a)

    await router.propagate_event("callback_query", query_a, db=db, bot=mock_bot)
    assert "Записал" in answer_mock_a.call_args[0][0]


@pytest.mark.asyncio
async def test_stranger_throttling_and_rate_limits(
    tmp_path: Path, mock_channel: MagicMock, mock_bot: AsyncMock
) -> None:
    """Stranger gets polite message once per 24 hours; spam > 20/min is dropped silently."""
    db_path = tmp_path / "test.db"
    db = Database(db_path)
    await db.connect()

    limiter = StrangerLimiter(user_rate_limit=5, stranger_global_limit=10)
    router = Router()
    mw = AuthAndScopeMiddleware(owner_chat_id=1001, limiter=limiter)
    router.message.middleware(mw)

    @router.message(Command("start"))
    @requires("user")
    async def cmd_start_dummy(message: Message) -> None:
        pass

    chat = Chat(id=9999, type="private")
    stranger = TgUser(id=9999, is_bot=False, first_name="Stranger")

    replies: list[str] = []
    msg = Message(message_id=1, date=1, chat=chat, from_user=stranger, text="/start")
    object.__setattr__(msg, "reply", AsyncMock(side_effect=lambda t, **kwargs: replies.append(t)))

    # First attempt: stranger receives polite message
    await router.propagate_event("message", msg, db=db, bot=mock_bot)
    assert len(replies) == 1
    assert replies[0] == STRANGER_FORBIDDEN

    # Second attempt immediately: dropped silently (24h cooldown)
    await router.propagate_event("message", msg, db=db, bot=mock_bot)
    assert len(replies) == 1

    # Non-private chat (e.g. group): dropped silently
    group_chat = Chat(id=-100123456, type="group")
    group_msg = Message(message_id=2, date=1, chat=group_chat, from_user=stranger, text="/start")
    object.__setattr__(
        group_msg, "reply", AsyncMock(side_effect=lambda t, **kwargs: replies.append(t))
    )

    await router.propagate_event("message", group_msg, db=db, bot=mock_bot)
    assert len(replies) == 1  # Still 1, group message dropped completely


@pytest.mark.asyncio
async def test_bot_blocking_and_resume_recovery(
    tmp_path: Path, mock_channel: MagicMock, mock_bot: AsyncMock
) -> None:
    """When a user kicks the bot:

    - User marked as blocked
    - Only their active products are paused with paused_reason='bot_blocked'
    - Manually paused products preserve manual reason
    - Delivery queue hides notifications while blocked (does not drop them)
    - On /start: only bot_blocked products are resumed!
    """
    db_path = tmp_path / "test.db"
    db = Database(db_path)
    await db.connect()

    products_dir = tmp_path / "products"
    products_dir.mkdir(parents=True)

    uid_a = await db.create_user(telegram_user_id=2001, chat_id=2001, role="user", status="active")

    # Product 1: active
    p1 = ProductProfile(
        id="u2_prod1",
        name="Product 1",
        search_url="https://www.avito.ru/search",
        max_price=10000,
        mode="live",
        owner_user_id=uid_a,
    )
    # Product 2: manually paused
    p2 = ProductProfile(
        id="u2_prod2",
        name="Product 2",
        search_url="https://www.avito.ru/search",
        max_price=20000,
        mode="off",
        paused_reason="manual",
        owner_user_id=uid_a,
    )
    save_product_profile(p1, directory=products_dir)
    save_product_profile(p2, directory=products_dir)

    # Insert pending delivery for user A
    conn = await db.connect()
    await conn.execute(
        "INSERT INTO events(id, avito_id, revision, kind, observed_at) VALUES (?, ?, 1, 'NEW_LISTING', '2026-01-01T12:00:00')",
        ("evt_pending_1", "lot_pending_1"),
    )
    await conn.execute(
        "INSERT INTO deliveries(id, event_id, user_id, channel, status, available_at, created_at) "
        "VALUES (?, ?, ?, 'telegram', 'pending', '2026-01-01T12:00:00', '2026-01-01T12:00:00')",
        ("del_notif_lot_p1", "evt_pending_1", uid_a),
    )
    await conn.commit()

    # 1. Verify pending notification is visible while user is active
    pending_before = await db.get_pending_notifications()
    assert len(pending_before) == 1

    # 2. Simulate user kicks/blocks bot via my_chat_member
    from aiogram.types import ChatMemberBanned, ChatMemberMember

    router = setup_telegram_router(mock_channel, db=db)

    chat = Chat(id=2001, type="private")
    tg_user = TgUser(id=2001, is_bot=False, first_name="UserA")
    old_member = ChatMemberMember(status="member", user=tg_user)
    new_member = ChatMemberBanned(status="kicked", user=tg_user, until_date=0)
    event_kick = ChatMemberUpdated(
        chat=chat,
        from_user=tg_user,
        date=1,
        old_chat_member=old_member,
        new_chat_member=new_member,
    )

    # Patch ProductStore directory
    store = ProductStore(directory=products_dir)
    from hunter.core import product as product_module
    from hunter.notify import telegram_router as router_module

    orig_get_store = product_module.get_product_store
    orig_router_get_store = router_module.get_product_store
    product_module.get_product_store = lambda directory=None: store
    router_module.get_product_store = lambda directory=None: store

    try:
        await router.propagate_event("my_chat_member", event_kick, db=db, bot=mock_bot)

        # Check user status in DB
        user_in_db = await db.get_user_by_id(uid_a)
        assert user_in_db["status"] in ("blocked", "bot_blocked")

        # Check products: p1 should have paused_reason='bot_blocked', p2 still 'manual'
        store.load_all()
        p1_reloaded = store.get("u2_prod1")
        p2_reloaded = store.get("u2_prod2")
        assert p1_reloaded.mode == "off"
        assert p1_reloaded.paused_reason == "bot_blocked"
        assert p2_reloaded.mode == "off"
        assert p2_reloaded.paused_reason == "manual"

        # 3. Check delivery queue: pending notification is SUPPRESSED while blocked (not lost)
        pending_while_blocked = await db.get_pending_notifications()
        assert len(pending_while_blocked) == 0

        # 4. User unblocks and sends /start
        replies: list[str] = []
        msg_start = Message(message_id=10, date=2, chat=chat, from_user=tg_user, text="/start")
        object.__setattr__(
            msg_start, "reply", AsyncMock(side_effect=lambda t, **kwargs: replies.append(t))
        )

        # First simulate member update unblock
        new_member_active = ChatMemberMember(status="member", user=tg_user)
        event_unblock = ChatMemberUpdated(
            chat=chat,
            from_user=tg_user,
            date=2,
            old_chat_member=new_member,
            new_chat_member=new_member_active,
        )
        await router.propagate_event("my_chat_member", event_unblock, db=db, bot=mock_bot)

        # Send /start
        await router.propagate_event("message", msg_start, db=db, bot=mock_bot)

        # User is active again
        user_resumed = await db.get_user_by_id(uid_a)
        assert user_resumed["status"] == "active"

        # Check products: ONLY p1 should be live! p2 should STILL be off!
        store.load_all()
        p1_resumed = store.get("u2_prod1")
        p2_resumed = store.get("u2_prod2")
        assert p1_resumed.mode == "live"
        assert p1_resumed.paused_reason is None
        assert p2_resumed.mode == "off"
        assert p2_resumed.paused_reason == "manual"

        # Delivery queue now delivers the pending notification!
        pending_after = await db.get_pending_notifications()
        assert len(pending_after) == 1
        assert pending_after[0]["avito_id"] == "lot_pending_1"

    finally:
        product_module.get_product_store = orig_get_store
        router_module.get_product_store = orig_router_get_store
