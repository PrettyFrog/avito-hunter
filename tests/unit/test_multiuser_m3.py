"""Multi-User Phase M3 Core Validation:
1. Price parsing table tests (>= 15 test cases + invalid cases)
2. URL extraction tests (extra words, mobile, short link rejection, foreign domains)
3. Bulk /pause and /resume without arguments (confirmation dialog & execution)
4. Alien callback security rejection
5. Public wizard state preservation on commit error
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import CallbackQuery, Chat, Message
from aiogram.types import User as TgUser

from hunter.core.db import Database
from hunter.core.product import (
    ProductProfile,
    ProductStore,
    save_product_profile,
)
from hunter.normalize.user_input import extract_avito_search_url, parse_price_input
from hunter.notify.messages_ru import (
    ADD_INVALID_URL,
    PAUSE_ALL_NONE,
)
from hunter.notify.security import GLOBAL_STRANGER_LIMITER
from hunter.notify.telegram_router import setup_telegram_router


@pytest.fixture(autouse=True)
def clean_test_env() -> Any:
    from hunter.core import product as product_module
    from hunter.notify import telegram_router as router_module

    orig_prod_store = product_module.get_product_store
    orig_router_store = router_module.get_product_store
    GLOBAL_STRANGER_LIMITER.reset()
    yield
    GLOBAL_STRANGER_LIMITER.reset()
    product_module.get_product_store = orig_prod_store
    router_module.get_product_store = orig_router_store


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
    bot.get_file = AsyncMock(return_value=MagicMock(file_path="mock_path.jpg"))
    bot.download_file = AsyncMock(return_value=None)
    return bot


def make_mock_message(
    chat_id: int,
    user: TgUser,
    text: str = "",
    bot: AsyncMock | None = None,
    message_id: int = 1,
    photo: list[PhotoSize] | None = None,
) -> Message:
    chat = Chat(id=chat_id, type="private")
    msg = Message(
        message_id=message_id,
        date=int(datetime.now(UTC).timestamp()),
        chat=chat,
        from_user=user,
        text=text,
        photo=photo,
    )
    if bot:
        msg.as_(bot)
    return msg


def make_mock_callback(
    chat_id: int,
    user: TgUser,
    data: str,
    message_text: str = "",
    bot: AsyncMock | None = None,
) -> CallbackQuery:
    chat = Chat(id=chat_id, type="private")
    msg = Message(
        message_id=1,
        date=int(datetime.now(UTC).timestamp()),
        chat=chat,
        from_user=user,
        text=message_text,
    )
    cb = CallbackQuery(
        id="cb_id_1",
        from_user=user,
        chat_instance="ci_1",
        message=msg,
        data=data,
    )
    if bot:
        cb.as_(bot)
        msg.as_(bot)
    object.__setattr__(cb, "answer", AsyncMock(return_value=True))
    object.__setattr__(msg, "edit_text", AsyncMock(return_value=True))
    object.__setattr__(msg, "reply", AsyncMock(return_value=True))
    return cb


# ==============================================================================
# 1. Price Parsing Table Tests (>= 15 Cases)
# ==============================================================================
def test_price_parsing_natural_formats() -> None:
    cases = [
        ("16000", 16000),
        ("16 000", 16000),
        ("16 000 ₽", 16000),
        ("16 000р", 16000),
        ("16 000 руб", 16000),
        ("16 000 рублей", 16000),
        ("16к", 16000),
        ("16k", 16000),
        ("16.5к", 16500),
        ("16,5к", 16500),
        ("16 тыс", 16000),
        ("16 тыс.", 16000),
        ("16 тысяч", 16000),
        ("16.5 тыс", 16500),
        ("16,5 тысяч", 16500),
        ("20 000р.", 20000),
        ("25 тыс.", 25000),
        ("500", 500),
        ("10000000", 10_000_000),
    ]
    for text, expected in cases:
        assert parse_price_input(text) == expected, f"Failed for input: {text}"


def test_price_parsing_invalid_cases() -> None:
    invalid_cases = [
        "",
        "   ",
        None,
        "0",
        "-500",
        "-16000",
        "10000001",  # > 10M
        "бесплатно",
        "договорная",
        "abc",
        "16 млн",
    ]
    for text in invalid_cases:
        with pytest.raises(ValueError):
            parse_price_input(text)


# ==============================================================================
# 2. URL Extraction Tests
# ==============================================================================
def test_extract_avito_search_url_valid() -> None:
    # Embedded in conversational sentence
    text1 = (
        "вот ссылка https://www.avito.ru/moskva/audio_i_video/naushniki?cd=1 посмотри пожалуйста"
    )
    url1, err1 = extract_avito_search_url(text1)
    assert err1 is None
    assert url1 == "https://www.avito.ru/moskva/audio_i_video/naushniki?cd=1"

    # Mobile URL
    text2 = "https://m.avito.ru/sankt-peterburg/tovary_dlya_kompyutera"
    url2, err2 = extract_avito_search_url(text2)
    assert err2 is None
    assert url2 == "https://m.avito.ru/sankt-peterburg/tovary_dlya_kompyutera"


def test_extract_avito_search_url_invalid_and_short() -> None:
    # Short link
    text_short = "https://www.avito.ru/s/abc12345"
    url, err = extract_avito_search_url(text_short)
    assert url is None
    assert "короткую ссылку" in err

    # Foreign domain
    text_foreign = "https://ozon.ru/product/sony-wh-1000xm6"
    url_f, err_f = extract_avito_search_url(text_foreign)
    assert url_f is None
    assert err_f == ADD_INVALID_URL

    # Empty
    url_e, err_e = extract_avito_search_url("")
    assert url_e is None
    assert err_e == ADD_INVALID_URL

# ==============================================================================
# 9. Bulk /pause and /resume (No Arguments)
# ==============================================================================
@pytest.mark.asyncio
async def test_bulk_pause_and_resume(
    tmp_path: Path, mock_channel: MagicMock, mock_bot: AsyncMock
) -> None:
    db_path = tmp_path / "test.db"
    db = Database(db_path)
    await db.connect()
    products_dir = tmp_path / "products"
    products_dir.mkdir(parents=True)
    store = ProductStore(directory=products_dir)

    from hunter.core import product as product_module
    from hunter.notify import telegram_router as router_module

    product_module.get_product_store = lambda directory=None: store
    router_module.get_product_store = lambda directory=None: store

    uid = await db.create_user(telegram_user_id=8001, chat_id=8001, role="user", status="active")
    user_tg = TgUser(id=8001, is_bot=False, first_name="BulkUser")
    router = setup_telegram_router(mock_channel, db=db)

    # Create 2 active products for user
    p1 = ProductProfile(
        id="u8001_p1",
        name="P1",
        search_url="https://avito.ru/1",
        max_price=5000,
        owner_user_id=uid,
        mode="live",
    )
    p2 = ProductProfile(
        id="u8001_p2",
        name="P2",
        search_url="https://avito.ru/2",
        max_price=6000,
        owner_user_id=uid,
        mode="live",
    )
    save_product_profile(p1, directory=products_dir)
    save_product_profile(p2, directory=products_dir)

    # 1. /pause without args
    replies: list[str] = []
    msg_pause = make_mock_message(chat_id=8001, user=user_tg, text="/pause", bot=mock_bot)
    object.__setattr__(msg_pause, "reply", AsyncMock(side_effect=lambda t, **kw: replies.append(t)))
    await router.propagate_event("message", msg_pause, db=db, bot=mock_bot)
    assert "Поставить на паузу все ваши товары (2 шт.)?" in replies[0]

    # Confirm bulk pause
    cb_pause_conf = make_mock_callback(
        chat_id=8001, user=user_tg, data="pause_all_confirm", bot=mock_bot
    )
    await router.propagate_event("callback_query", cb_pause_conf, db=db, bot=mock_bot)
    assert store.get("u8001_p1").mode == "off"
    assert store.get("u8001_p2").mode == "off"

    # Pause again when all already paused
    replies.clear()
    await router.propagate_event("message", msg_pause, db=db, bot=mock_bot)
    assert replies[0] == PAUSE_ALL_NONE

    # 2. /resume without args
    replies.clear()
    msg_resume = make_mock_message(chat_id=8001, user=user_tg, text="/resume", bot=mock_bot)
    object.__setattr__(
        msg_resume, "reply", AsyncMock(side_effect=lambda t, **kw: replies.append(t))
    )
    await router.propagate_event("message", msg_resume, db=db, bot=mock_bot)
    assert "Возобновить поиск по всем вашим товарам (2 шт.)?" in replies[0]

    # Confirm bulk resume
    cb_res_conf = make_mock_callback(
        chat_id=8001, user=user_tg, data="resume_all_confirm", bot=mock_bot
    )
    edits: list[str] = []
    object.__setattr__(
        cb_res_conf.message, "edit_text", AsyncMock(side_effect=lambda t, **kw: edits.append(t))
    )
    await router.propagate_event("callback_query", cb_res_conf, db=db, bot=mock_bot)
    assert store.get("u8001_p1").mode == "live"
    assert store.get("u8001_p2").mode == "live"
    assert edits and "(2 шт.)" in edits[0]



@pytest.mark.asyncio
async def test_alien_callback_all_variants_rejected(
    tmp_path: Path, mock_channel: MagicMock, mock_bot: AsyncMock
) -> None:
    db = Database(tmp_path / "alien.db")
    await db.connect()
    products_dir = tmp_path / "products"
    products_dir.mkdir(parents=True)
    store = ProductStore(directory=products_dir)

    from hunter.core import product as product_module
    from hunter.notify import telegram_router as router_module

    product_module.get_product_store = lambda directory=None: store
    router_module.get_product_store = lambda directory=None: store

    uid_a = await db.create_user(telegram_user_id=7201, chat_id=7201, role="user", status="active")
    await db.create_user(telegram_user_id=7202, chat_id=7202, role="user", status="active")
    user_b = TgUser(id=7202, is_bot=False, first_name="UserB")

    p = ProductProfile(
        id="u7201_alien",
        name="Alien Item",
        search_url="https://avito.ru/item",
        max_price=10000,
        owner_user_id=uid_a,
        mode="live",
    )
    save_product_profile(p, directory=products_dir)
    router = setup_telegram_router(mock_channel, db=db)

    callbacks = [
        f"my_pause:{p.id}",
        f"my_resume:{p.id}",
        f"my_price:{p.id}",
        f"my_del_ask:{p.id}",
        f"my_del_do:{p.id}",
        f"pause_prod:{p.id}",
    ]
    for cb_data in callbacks:
        cb = make_mock_callback(chat_id=7202, user=user_b, data=cb_data, bot=mock_bot)
        await router.propagate_event("callback_query", cb, db=db, bot=mock_bot)
        assert cb.answer.called

    reloaded = store.get(p.id)
    assert reloaded is not None
    assert reloaded.mode == "live"
    assert reloaded.max_price == 10000
    assert (products_dir / f"{p.id}.yaml").exists()


@pytest.mark.asyncio
async def test_public_wizard_preserves_state_on_commit_error(tmp_path: Path) -> None:
    from types import SimpleNamespace

    db = Database(tmp_path / "wizard_err.db")
    await db.connect()
    uid = await db.create_user(telegram_user_id=8801, chat_id=8801, role="user", status="active")
    await db.save_pending_product(
        8801, "public_add_words", {"url": "https://avito.ru/item", "price": 5000}
    )

    class FailingService:
        async def add_subscription(self, **kwargs):
            raise RuntimeError("Database connection lost")

    replies: list[str] = []

    class MockMsg:
        chat = SimpleNamespace(id=8801)

        async def reply(self, text, **kwargs):
            replies.append(text)

    from hunter.notify.telegram_handlers.public_add import _finish_public_add

    user = SimpleNamespace(id=uid, role="user")
    await _finish_public_add(
        MockMsg(), db, user, "https://avito.ru/item", 5000, "", sub_service=FailingService()
    )

    pending = await db.get_pending_product(8801)
    assert pending is not None
    assert "Ошибка сохранения подписки" in replies[0]

    class SuccessService:
        async def add_subscription(self, **kwargs):
            return SimpleNamespace(max_price=5000, manual_stopwords="", query_id="q123")

    replies.clear()
    await _finish_public_add(
        MockMsg(), db, user, "https://avito.ru/item", 5000, "", sub_service=SuccessService()
    )
    assert await db.get_pending_product(8801) is None
    assert "Подписка успешно добавлена" in replies[0]
