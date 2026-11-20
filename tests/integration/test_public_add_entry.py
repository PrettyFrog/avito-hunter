"""Public add must not enter the legacy AI wizard via commands or buttons."""
import pytest
from aiogram.types import User as TgUser
from tests.unit.test_public_add_flow import _init_env, _make_cb, _make_msg

from hunter.application.cutover import CutoverMode
from hunter.notify.security import AuthAndScopeMiddleware, StrangerLimiter


@pytest.mark.asyncio
@pytest.mark.parametrize("via_button", [False, True])
@pytest.mark.parametrize("mode", [None, CutoverMode.V2, CutoverMode.LEGACY, CutoverMode.DUAL_EVAL])
async def test_public_add_entry_is_zero_llm_or_denied(tmp_path, mode, via_button):
    db, writer, _, service, cm, router, bot = await _init_env(tmp_path)
    service.auto_assign = True
    for middleware in router.message.outer_middleware:
        if isinstance(middleware, AuthAndScopeMiddleware):
            middleware.limiter = StrangerLimiter()
    try:
        uid = await db.create_user(telegram_user_id=101, chat_id=101, role="user", status="active")
        if mode is not None:
            await cm.set_tenant_mode(uid, mode)
        tg = TgUser(id=101, is_bot=False, first_name="One")
        if via_button:
            event = _make_cb(101, tg, "btn_start_add", bot)
            await router.propagate_event("callback_query", event, db=db, bot=bot)
        else:
            event = _make_msg(101, tg, "/add", bot)
            await router.propagate_event("message", event, db=db, bot=bot)
        state = await db.get_pending_product(101)
        assert state and state["step"] == "public_add_name"
        assert await cm.is_v2_publisher(uid)
    finally:
        await writer.stop()
        await db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [None, CutoverMode.V2])
async def test_owner_keeps_explicit_public_or_legacy_choice(tmp_path, mode):
    from hunter.core.models import User
    from hunter.notify.telegram_handlers.public_add_entry import maybe_start_public_add

    db, writer, _, service, cm, _, bot = await _init_env(tmp_path)
    service.auto_assign = True
    try:
        uid = await db.create_user(telegram_user_id=101, chat_id=101, role="owner", status="active")
        if mode is not None:
            await cm.set_tenant_mode(uid, mode)
        owner = User(id=uid, telegram_user_id=101, chat_id=101, role="owner",
                     status="active", created_at="")
        event = _make_msg(101, TgUser(id=101, is_bot=False, first_name="Owner"), "/add", bot)
        handled = await maybe_start_public_add(event, owner, db)
        assert handled is True
    finally:
        await writer.stop()
        await db.close()
