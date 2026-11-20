from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import Chat, ChatMemberBanned, ChatMemberMember, ChatMemberUpdated, Message
from aiogram.types import User as TgUser

from hunter.core.db import Database
from hunter.core.product import (
    ProductProfile,
    ProductStore,
    apply_product_pause,
    clear_product_pause,
    save_product_profile,
)
from hunter.notify.messages_ru import USER_BLOCKED_BY_ADMIN
from hunter.notify.telegram_router import setup_telegram_router


@pytest.mark.asyncio
async def test_pause_priority_and_cannot_downgrade(tmp_path: Path) -> None:
    """Verifies pause priority hierarchy: owner_paused (3) > bot_blocked (2) > user_paused (1).
    A lower-priority pause cannot overwrite a higher-priority pause.
    """
    p = ProductProfile(
        id="test_hier_1",
        name="Hierarchy Test",
        search_url="https://avito.ru",
        max_price=10000,
        owner_user_id=10,
    )

    # 1. Normal state -> user_paused
    assert apply_product_pause(p, "user_paused", actor_role="user") is True
    assert p.mode == "off"
    assert p.paused_reason == "user_paused"

    # 2. user_paused -> bot_blocked (higher priority, allowed)
    assert apply_product_pause(p, "bot_blocked") is True
    assert p.paused_reason == "bot_blocked"

    # 3. bot_blocked -> user_paused (lower priority, must be rejected)
    assert apply_product_pause(p, "user_paused", actor_role="user") is False
    assert p.paused_reason == "bot_blocked"

    # 4. bot_blocked -> owner_paused (highest priority, allowed)
    assert apply_product_pause(p, "owner_paused", actor_role="owner") is True
    assert p.paused_reason == "owner_paused"

    # 5. owner_paused -> bot_blocked (lower priority, must be rejected)
    assert apply_product_pause(p, "bot_blocked") is False
    assert p.paused_reason == "owner_paused"

    # 6. owner_paused -> user_paused (lower priority, must be rejected)
    assert apply_product_pause(p, "user_paused", actor_role="user") is False
    assert p.paused_reason == "owner_paused"

    # 7. Clearing pause: user cannot clear owner_paused
    ok, msg = clear_product_pause(p, actor_role="user")
    assert ok is False
    assert p.mode == "off"
    assert p.paused_reason == "owner_paused"

    # 8. Clearing pause: owner CAN clear owner_paused
    ok, msg = clear_product_pause(p, actor_role="owner")
    assert ok is True
    assert p.mode == "live"
    assert p.paused_reason is None


@pytest.mark.asyncio
async def test_blocked_by_owner_vs_bot_blocked_and_start_behavior(tmp_path: Path) -> None:
    """Verifies that:
    - blocked_by_owner is distinct from bot_blocked.
    - /start does NOT unblock a user with status='blocked_by_owner'.
    - /start DOES reactivate a user with status='bot_blocked'.
    - Unblocking bot in Telegram does NOT unblock a user who was blocked by owner.
    """
    db_path = tmp_path / "test.db"
    db = Database(db_path)
    await db.connect()

    owner_id = await db.create_user(
        telegram_user_id=1001, chat_id=1001, role="owner", status="active"
    )
    user_tg_id = 3003
    user_chat_id = 3003
    user_id = await db.create_user(
        telegram_user_id=user_tg_id,
        chat_id=user_chat_id,
        role="user",
        status="active",
        tier="friend",
    )

    products_dir = tmp_path / "products"
    products_dir.mkdir(parents=True, exist_ok=True)
    store = ProductStore(directory=products_dir)

    from hunter.core import product as product_module
    from hunter.notify import telegram_router as router_module

    product_module.get_product_store = lambda directory=None: store
    router_module.get_product_store = lambda directory=None: store

    p = ProductProfile(
        id="prod_u3",
        name="User 3 Product",
        search_url="https://avito.ru",
        max_price=12000,
        owner_user_id=user_id,
        mode="live",
    )
    save_product_profile(p, directory=products_dir)

    mock_channel = MagicMock()
    mock_channel.alert = AsyncMock()
    mock_bot = AsyncMock()

    router = setup_telegram_router(mock_channel, db=db)
    tg_user = TgUser(id=user_tg_id, is_bot=False, first_name="User3")
    chat = Chat(id=user_chat_id, type="private")

    # Scenario A: User is blocked by owner
    await db.block_user_by_owner(user_id)
    u_db = await db.get_user_by_id(user_id)
    assert u_db["status"] == "blocked_by_owner"

    # User attempts /start
    replies_start: list[str] = []
    msg_start = Message(message_id=1, date=1, chat=chat, from_user=tg_user, text="/start")
    object.__setattr__(
        msg_start, "reply", AsyncMock(side_effect=lambda t, **kw: replies_start.append(t))
    )

    await router.propagate_event("message", msg_start, db=db, bot=mock_bot)
    assert len(replies_start) == 1
    assert replies_start[0] == USER_BLOCKED_BY_ADMIN

    # User is STILL blocked_by_owner
    u_db = await db.get_user_by_id(user_id)
    assert u_db["status"] == "blocked_by_owner"

    # Simulating chat member update to "member": must NOT unblock owner-blocked user!
    old_member = ChatMemberBanned(status="kicked", user=tg_user, until_date=0)
    new_member = ChatMemberMember(status="member", user=tg_user)
    event_member = ChatMemberUpdated(
        chat=chat,
        from_user=tg_user,
        date=2,
        old_chat_member=old_member,
        new_chat_member=new_member,
    )
    await router.propagate_event("my_chat_member", event_member, db=db, bot=mock_bot)

    u_db = await db.get_user_by_id(user_id)
    assert u_db["status"] == "blocked_by_owner"

    # Scenario B: Unblock by owner
    await db.unblock_user_by_owner(user_id)
    u_db = await db.get_user_by_id(user_id)
    assert u_db["status"] == "active"

    # Scenario C: Bot blocked by user (my_chat_member kicked)
    event_kick = ChatMemberUpdated(
        chat=chat,
        from_user=tg_user,
        date=3,
        old_chat_member=new_member,
        new_chat_member=old_member,
    )
    await router.propagate_event("my_chat_member", event_kick, db=db, bot=mock_bot)

    u_db = await db.get_user_by_id(user_id)
    assert u_db["status"] == "bot_blocked"
    assert u_db["status"] != "blocked_by_owner"

    # Now user unblocks bot (/start or member event)
    await router.propagate_event("my_chat_member", event_member, db=db, bot=mock_bot)
    u_db = await db.get_user_by_id(user_id)
    assert u_db["status"] == "active"
