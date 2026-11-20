"""A public search starts with its tenant-specific display name."""
from pathlib import Path

import pytest
from aiogram.types import User as TgUser
from tests.unit.test_public_add_flow import _init_env, _make_cb, _make_msg

from hunter.application.cutover import CutoverMode


@pytest.mark.asyncio
async def test_name_first_is_saved_for_both_stopword_paths(tmp_path: Path):
    db, writer, repo, service, cutover, router, bot = await _init_env(tmp_path)
    try:
        uid = await db.create_user(telegram_user_id=101, chat_id=101, role="user", status="active")
        await cutover.set_tenant_mode(uid, CutoverMode.V2)
        user = TgUser(id=101, is_bot=False, first_name="User")
        async def send(text):
            msg = _make_msg(101, user, text, bot)
            await router.propagate_event("message", msg, db=db, bot=bot)
            return msg
        await send("/add")
        assert (await db.get_pending_product(101))["step"] == "public_add_name"
        await send("x" * 101)
        assert (await db.get_pending_product(101))["step"] == "public_add_name"
        await send("Мой ноутбук")
        assert (await db.get_pending_product(101))["step"] == "public_add_url"
        await send("https://www.avito.ru/moskva?q=mac")
        await send("50000")
        await send("битый")
        first = (await service.list_subscriptions(uid))[0]
        assert first.name == "Мой ноутбук" and first.manual_stopwords == "битый"
        await send("/add")
        await send("Телефон")
        await send("https://www.avito.ru/moskva?q=iphone")
        await send("40000")
        await router.propagate_event(
            "callback_query", _make_cb(101, user, "public_add_skip_words", bot), db=db, bot=bot
        )
        names = {item.name for item in await service.list_subscriptions(uid)}
        assert names == {"Мой ноутбук", "Телефон"}
    finally:
        await writer.stop()
        await db.close()
