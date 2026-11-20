import pytest
from tests.unit.test_public_status import app_setup as app_setup

from hunter.core.models import User
from hunter.notify.telegram_handlers.public_search_menu import (
    search_card,
    search_list_page,
    search_token,
)
from hunter.storage.subscriptions import SubscriptionActor


@pytest.mark.asyncio
async def test_list_keyboard_uses_names_and_is_scoped(app_setup):
    _, repo, db = app_setup
    await repo.create(SubscriptionActor(1), "sub_1", 1, "q1", 10000, name="Mac <один>")
    await repo.create(SubscriptionActor(2), "sub_2", 2, "q2", 20000, name="Чужой")
    user = User(id=1, telegram_user_id=1, chat_id=1, role="owner", created_at="")
    text, keyboard = await search_list_page(user, db, 0)
    assert "Mac &lt;один&gt;" in text and "Чужой" not in text
    buttons = [btn for row in keyboard.inline_keyboard for btn in row]
    assert any(
        btn.text == "Mac <один>" and btn.callback_data == f"sm:view:{search_token('sub_1')}"
        for btn in buttons
    )
    assert all(len(btn.callback_data.encode()) <= 64 for btn in buttons)


@pytest.mark.asyncio
async def test_card_is_tenant_scoped_and_escapes_html(app_setup):
    _, repo, db = app_setup
    await repo.create(SubscriptionActor(1), "sub_1", 1, "q1", 10000, "<бой>", name="<Mac>")
    user = User(id=1, telegram_user_id=1, chat_id=1, role="user", created_at="")
    other = User(id=2, telegram_user_id=2, chat_id=2, role="owner", created_at="")
    text, keyboard = await search_card(user, db, "sub_1")
    assert "&lt;Mac&gt;" in text and "&lt;бой&gt;" in text and "10 000" in text
    assert keyboard.inline_keyboard
    assert await search_card(other, db, "sub_1") is None


@pytest.mark.asyncio
async def test_card_button_layout_has_stopwords_and_keywords_in_one_row(app_setup):
    _, repo, db = app_setup
    await repo.create(
        SubscriptionActor(1), "sub_kw", 1, "q1", 15000, "дефект", name="DDR4", keywords="crucial,16gb"
    )
    user = User(id=1, telegram_user_id=1, chat_id=1, role="user", created_at="")
    text, keyboard = await search_card(user, db, "sub_kw")
    assert "Ключевые слова: crucial,16gb" in text
    assert "Стоп-слова: дефект" in text
    # Verify row with both buttons: stop-words left, keywords right
    row2 = keyboard.inline_keyboard[1]
    assert len(row2) == 2
    assert row2[0].text == "🚫 Стоп-слова"
    assert "words" in row2[0].callback_data
    assert row2[1].text == "🔑 Ключевые слова"
    assert "keywords" in row2[1].callback_data


@pytest.mark.asyncio
async def test_pagination_and_empty_list(app_setup):
    _, repo, db = app_setup
    user = User(id=1, telegram_user_id=1, chat_id=1, role="user", created_at="")
    text, kb = await search_list_page(user, db)
    assert "Пока нет поисков" in text and not kb.inline_keyboard
    for i in range(12):
        await repo.create(SubscriptionActor(1), f"sub_{i}", 1, f"q{i}", 10000, name=f"Поиск {i}")
    _, first = await search_list_page(user, db)
    _, last = await search_list_page(user, db, 99)
    assert any(b.callback_data == "sm:page:1" for row in first.inline_keyboard for b in row)
    assert any(b.callback_data == "sm:page:0" for row in last.inline_keyboard for b in row)
    assert len(last.inline_keyboard) == 3
    assert await search_card(user, db, "missing") is None


@pytest.mark.asyncio
async def test_long_values_are_bounded_for_telegram(app_setup):
    _, repo, db = app_setup
    await repo.create(
        SubscriptionActor(1),
        "long",
        1,
        "q1",
        10000,
        "😀" * 5000,
        "https://avito.ru/" + "😀" * 5000,
        name="Имя",
    )
    user = User(id=1, telegram_user_id=1, chat_id=1, role="user", created_at="")
    text, _ = await search_card(user, db, "long")
    assert len(text.encode("utf-16-le")) // 2 < 4096
    assert text.count("…") == 2
