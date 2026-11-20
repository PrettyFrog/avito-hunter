"""Core scenario integration tests on simulated stand (<= 400 lines)."""

from __future__ import annotations

import asyncio
import pytest

from tools.stand import StandContext, create_stand


@pytest.mark.asyncio
async def test_sc01_sc03_start_flows() -> None:
    """SC-01 (Owner /start), SC-03 (Stranger /start and req_access)."""
    stand = await create_stand(owner_chat_id=111111, stranger_chat_id=999999)
    try:
        # SC-01: Owner /start
        await stand.owner.send_command("start")
        msg = await stand.owner.wait_for_message(timeout=4.0)
        assert "панель владельца" in msg.text.lower() or "главное меню" in msg.text.lower()

        # SC-03: Stranger /start
        await stand.stranger.send_command("start")
        stranger_msg = await stand.stranger.wait_for_message(timeout=4.0)
        assert "закрытый бот" in stranger_msg.text.lower()

        # Stranger clicks 'req_access'
        buttons = dict(stranger_msg.find_buttons())
        assert "req_access" in buttons.values()
        await stand.stranger.click("req_access", message=stranger_msg)
        await asyncio.sleep(0.5)

        # Owner receives notification of access request
        owner_msgs = stand.owner.messages
        assert any("просит доступ" in m.text.lower() or "запрос" in m.text.lower() for m in owner_msgs)
    finally:
        await stand.stop()


@pytest.mark.asyncio
async def test_sc02_invite_onboarding_flow() -> None:
    """SC-02: Owner creates invite, new user registers via invite token."""
    stand = await create_stand(owner_chat_id=111111, stranger_chat_id=999999)
    try:
        # 1. Owner creates invite
        await stand.owner.send_command("invite", "Alice friend")
        invite_msg = await stand.owner.wait_for_message(timeout=4.0)
        assert "приглашение создано" in invite_msg.text.lower()
        # Extract token: https://t.me/...start=inv_<token>
        match = [part for part in invite_msg.text.split() if "start=inv_" in part]
        assert match, f"Invite link not found in {invite_msg.text}"
        token = match[0].split("start=inv_")[-1]

        # 2. Alice sends /start inv_<token>
        alice = stand.tg.get_user(user_id=333333, chat_id=333333, username="alice_wonder")
        await alice.send_command("start", f"inv_{token}")
        welcome_msg = await alice.wait_for_message(timeout=4.0)
        assert "добро пожаловать" in welcome_msg.text.lower() or "привет" in welcome_msg.text.lower()

        # Check DB user was created with role='user'
        user_row = await stand.db.get_user_by_telegram_id(333333)
        assert user_row is not None
        assert user_row["role"] == "user"
        assert user_row["status"] == "active"
    finally:
        await stand.stop()


@pytest.mark.asyncio
async def test_sc04_help_and_faq_flow() -> None:
    """SC-04: /help and /faq navigation with interactive buttons."""
    stand = await create_stand(owner_chat_id=111111)
    try:
        # /help
        await stand.owner.send_command("help")
        help_msg = await stand.owner.wait_for_message(timeout=4.0)
        assert "справка" in help_msg.text.lower() or "/my" in help_msg.text

        # /faq
        await stand.owner.send_command("faq")
        faq_msg = await stand.owner.wait_for_message(timeout=4.0)
        buttons = dict(faq_msg.find_buttons())
        assert any(cb.startswith("faq:") for cb in buttons.values())

        # Click first FAQ item
        first_faq_cb = [cb for cb in buttons.values() if cb.startswith("faq:")][0]
        await stand.owner.click(first_faq_cb, message=faq_msg)
        await asyncio.sleep(0.5)

        # Check back button exists
        cur_msg = stand.owner.last_message
        assert cur_msg is not None
        cur_buttons = dict(cur_msg.find_buttons())
        assert "faq_menu" in cur_buttons.values()
    finally:
        await stand.stop()


@pytest.mark.asyncio
async def test_sc05_to_sc09_subscription_lifecycle() -> None:
    """SC-05 (/add wizard), SC-06 (/my), SC-07, SC-08 (pause), SC-09 (delete)."""
    stand = await create_stand(owner_chat_id=111111)
    try:
        # SC-05: /add public wizard
        await stand.owner.send_command("add")
        step1 = await stand.owner.wait_for_message(timeout=4.0)
        assert "шаг 1" in step1.text.lower() or "назвать" in step1.text.lower()

        # Step 1: name
        await stand.owner.send_text("Sony A6000 Black")
        step2 = await stand.owner.wait_for_message(timeout=4.0)
        assert "шаг 2" in step2.text.lower() or "ссылку" in step2.text.lower()

        # Step 2: URL
        await stand.owner.send_text("https://www.avito.ru/moskva/fototehnika?q=sony+a6000")
        step3 = await stand.owner.wait_for_message(timeout=4.0)
        assert "шаг 3" in step3.text.lower() or "цену" in step3.text.lower()

        # Step 3: max price
        await stand.owner.send_text("45000")
        step4 = await stand.owner.wait_for_message(timeout=4.0)
        assert "шаг 4" in step4.text.lower() or "минус" in step4.text.lower() or "стоп" in step4.text.lower()

        # Step 4: skip stop-words
        await stand.owner.click("public_add_skip_words", message=step4)
        final_add = await stand.owner.wait_for_message(timeout=4.0)
        assert "подписка успешно добавлена" in final_add.text.lower() or "добавлена" in final_add.text.lower()

        # SC-06: /my
        await stand.owner.send_command("my")
        my_msg = await stand.owner.wait_for_message(timeout=4.0)
        assert "sony a6000 black" in my_msg.text.lower() or len(my_msg.find_buttons()) > 0

        # Check DB subscription was registered
        cur = await stand.db._conn.execute("SELECT * FROM subscriptions")
        subs = await cur.fetchall()
        assert len(subs) >= 1

        # SC-08: Pause subscription
        await stand.owner.send_command("pause", "sony_a6000")
        await asyncio.sleep(0.5)

        # SC-09: Delete with confirmation
        await stand.owner.send_command("delete_me")
        del_msg = await stand.owner.wait_for_message(timeout=4.0)
        # Owner cannot delete themselves
        assert "не может удалить" in del_msg.text.lower() or "владелец" in del_msg.text.lower()
    finally:
        await stand.stop()


@pytest.mark.asyncio
async def test_sc14_sc16_support_admin_delete_user() -> None:
    """SC-14 (/support), SC-15 (/admin), SC-16 (/delete_me for non-owner)."""
    stand = await create_stand(owner_chat_id=111111)
    try:
        # Create user Bob
        now_iso = "2026-10-31T02:00:00Z"
        await stand.db._conn.execute(
            "INSERT INTO users (id, telegram_user_id, chat_id, username, role, status, created_at) "
            "VALUES (2, 555555, 555555, 'bob_user', 'user', 'active', ?)",
            (now_iso,),
        )
        await stand.db._conn.commit()
        bob = stand.tg.get_user(user_id=555555, chat_id=555555, username="bob_user")

        # SC-14: Bob sends /support
        await bob.send_command("support")
        prompt_msg = await bob.wait_for_message(timeout=4.0)
        assert "опишите ваш вопрос" in prompt_msg.text.lower()
        await bob.send_text("Как настроить уведомления?")
        bob_reply = await bob.wait_for_message(timeout=4.0)
        assert "обращение" in bob_reply.text.lower() or "отправлено" in bob_reply.text.lower()
        # Owner gets support alert
        t0 = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - t0 < 5.0:
            if any("bob_user" in m.text or "обращение" in m.text.lower() for m in stand.owner.messages):
                break
            await asyncio.sleep(0.05)
        assert any("bob_user" in m.text or "обращение" in m.text.lower() for m in stand.owner.messages)

        # SC-15: Owner /admin
        await stand.owner.send_command("admin")
        admin_msg = await stand.owner.wait_for_message(timeout=4.0)
        assert "панель управления" in admin_msg.text.lower() or "владелец" in admin_msg.text.lower()

        # SC-16: Bob sends /delete_me
        await bob.send_command("delete_me")
        bob_del_msg = await bob.wait_for_message(timeout=4.0)
        buttons = dict(bob_del_msg.find_buttons())
        assert "delme:confirm" in buttons.values()

        # Bob confirms deletion
        await bob.click("delme:confirm", message=bob_del_msg)
        await asyncio.sleep(0.5)

        # Verify Bob is marked deleted in DB
        bob_row = await stand.db.get_user_by_id(2)
        assert bob_row is not None and bob_row["status"] == "deleted"
        by_tg = await stand.db.get_user_by_telegram_id(555555)
        assert by_tg is None
    finally:
        await stand.stop()
