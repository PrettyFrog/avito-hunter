from __future__ import annotations

import html
from pathlib import Path
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Chat, Message, User as TgUser

from hunter.core.models import User
from hunter.core.product import (
    ProductCoverage,
    ProductMatch,
    ProductPhotos,
    ProductProfile,
    ProductStore,
)
from hunter.notify.telegram_router import _safe_edit_text, _safe_reply, setup_telegram_router


def make_mock_message(text: str, user_id: int = 1001, username: str = "owner") -> Message:
    tg_user = TgUser(id=user_id, is_bot=False, first_name="Test", username=username)
    chat = Chat(id=user_id, type="private")
    msg = Message(
        message_id=1,
        date=1700000000,
        chat=chat,
        from_user=tg_user,
        text=text,
    )
    object.__setattr__(msg, "reply", AsyncMock())
    return msg


@pytest.mark.asyncio
async def test_safe_reply_retries_without_parse_mode_on_entity_error() -> None:
    msg = make_mock_message("test")
    # First call raises TelegramBadRequest (entity parse error)
    # Second call succeeds
    reply_mock = AsyncMock(
        side_effect=[
            TelegramBadRequest(
                method=MagicMock(),
                message="Bad Request: can't parse entities: Can't find end of the entity",
            ),
            MagicMock(),
        ]
    )
    object.__setattr__(msg, "reply", reply_mock)

    await _safe_reply(msg, "text with _unmatched_ *formatting", parse_mode="Markdown")

    assert reply_mock.call_count == 2
    # First call had parse_mode="Markdown"
    assert reply_mock.call_args_list[0].kwargs.get("parse_mode") == "Markdown"
    # Second call fell back to parse_mode=None
    assert reply_mock.call_args_list[1].kwargs.get("parse_mode") is None


@pytest.mark.asyncio
async def test_safe_edit_text_retries_without_parse_mode_on_entity_error() -> None:
    target = MagicMock()
    target.edit_text = AsyncMock(
        side_effect=[
            TelegramBadRequest(method=MagicMock(), message="Bad Request: can't parse entities"),
            MagicMock(),
        ]
    )

    await _safe_edit_text(target, "bad *entity text", parse_mode="Markdown")

    assert target.edit_text.call_count == 2
    assert target.edit_text.call_args_list[0].kwargs.get("parse_mode") == "Markdown"
    assert target.edit_text.call_args_list[1].kwargs.get("parse_mode") is None


@pytest.mark.asyncio
async def test_safe_reply_raises_other_bad_request_errors() -> None:
    msg = make_mock_message("test")
    reply_mock = AsyncMock(
        side_effect=TelegramBadRequest(method=MagicMock(), message="Bad Request: chat not found")
    )
    object.__setattr__(msg, "reply", reply_mock)

    with pytest.raises(TelegramBadRequest, match="chat not found"):
        await _safe_reply(msg, "hello", parse_mode="Markdown")


@pytest.mark.asyncio
async def test_cmd_show_formats_urls_with_single_underscore_safely(tmp_path: Path) -> None:
    # Product with single underscore in URL (like bytovaya_elektronika)
    prod = ProductProfile(
        id="sony_xm6",
        name="Sony WH-1000XM6",
        search_url="https://www.avito.ru/all/bytovaya_elektronika?q=sony+wh+1000+xm+6",
        max_price=16000,
        headroom_pct=0.2,
        mode="live",
        priority="high",
        match=ProductMatch(policy="strict", aliases=["sony_wh1000xm6"]),
        photos=ProductPhotos(check="both", refs=["ref1.jpg"]),
        coverage=ProductCoverage(),
    )

    store = ProductStore(directory=tmp_path)
    store._cache = {prod.id: prod}
    store._profiles = {prod.id: prod}

    channel = MagicMock()
    channel.chat_id = "1001"
    router = setup_telegram_router(channel)

    msg = make_mock_message("/show sony_xm6", user_id=1001)

    with patch("hunter.notify.telegram_router.ProductStore", return_value=store):
        # Find cmd_show handler
        for handler in router.message.handlers:
            fn = handler.callback
            if getattr(fn, "__name__", "") == "cmd_show":
                owner_user = User(
                    id=1,
                    telegram_user_id=1001,
                    chat_id=1001,
                    role="owner",
                    created_at="2026-01-01T00:00:00",
                )
                await fn(msg, current_user=owner_user)
                break

    assert msg.reply.called
    call_args = msg.reply.call_args
    # Must use HTML parse_mode to handle underscores safely
    assert call_args.kwargs.get("parse_mode") == "HTML"
    text = call_args.args[0]
    assert f'<a href="{html.escape(prod.search_url)}">Открыть на Авито</a>' in text
    assert "<code>sony_xm6</code>" in text
    assert "<b>Товар:</b>" in text


def test_escape_telegram_html_and_delivery_formatting() -> None:
    from hunter.notify.telegram_delivery import escape_telegram_html, format_delivery_text

    raw = "Apple <iPhone> 16 & 'Pro' Max"
    escaped = escape_telegram_html(raw)
    assert "&lt;iPhone&gt;" in escaped and "&amp;" in escaped
    text = format_delivery_text(raw, 90000, "https://avito.ru/1", old_price=100000, is_html=True)
    assert "&lt;iPhone&gt;" in text and "90000" in text and "100000" in text


def test_to_short_avito_url_comprehensive() -> None:
    from hunter.notify.telegram_delivery import to_short_avito_url, format_delivery_text

    # Case 1: Long URL with context and city/category
    long_url = (
        "https://www.avito.ru/sterlitamak/tovary_dlya_kompyutera/"
        "ddr4ddr3_s_oshibkami_8432432801?context=H4sIAAAAAAAA_wE_AMD_YToyOntzOjEzOiJsb2NhbFByaW9yaXR5IjtiOjA"
    )
    assert to_short_avito_url(long_url) == "https://www.avito.ru/8432432801"
    assert to_short_avito_url(long_url, avito_id="8432432801") == "https://www.avito.ru/8432432801"

    # Case 2: URL with slocation and context
    url_slocation = (
        "https://www.avito.ru/balashiha/tovary_dlya_kompyutera/"
        "operativnaya_pamyat_ddr4_8gb_3200mhz_8589673944?slocation=653240&context=abc"
    )
    assert to_short_avito_url(url_slocation) == "https://www.avito.ru/8589673944"

    # Case 3: Already short URL
    already_short = "https://www.avito.ru/8589673944"
    assert to_short_avito_url(already_short) == "https://www.avito.ru/8589673944"

    # Case 4: Mobile URL
    mobile_url = "https://m.avito.ru/moskva/telefony/item_8589673944"
    assert to_short_avito_url(mobile_url) == "https://www.avito.ru/8589673944"

    # Case 5: format_delivery_text produces short URL
    formatted = format_delivery_text("DDR4", 1500, long_url, avito_id="8432432801")
    assert "https://www.avito.ru/8432432801" in formatted
    assert "context=" not in formatted

    # Case 6: Fallback for test/mock non-Avito URLs
    assert to_short_avito_url("https://avito.ru/1") == "https://avito.ru/1"
    assert to_short_avito_url("") == ""

