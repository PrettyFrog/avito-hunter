from __future__ import annotations

from datetime import datetime, timezone
import pytest
from hunter.notify.telegram_alert_card import (
    _format_price_header, build_alert_keyboard, format_alert_caption,
    format_price_rub, format_relative_time, format_rich_html,
    format_seller_line,
)


@pytest.mark.parametrize("p,expected", [(None, "Цена не указана"), (0, "0 ₽"), (22500, "22 500 ₽"), (1000000, "1 000 000 ₽")])
def test_format_price_rub(p: int | None, expected: str) -> None:
    assert format_price_rub(p) == expected


def test_format_seller_line() -> None:
    full = format_seller_line("Александр", "5.0", "32 отзыва", "24 завершённых")
    assert full == "👤 Александр · ⭐ 5,0 (32 отзыва) · 24 завершённых"
    priv = format_seller_line(None, seller_type="private")
    assert priv == "👤 Частное лицо"
    assert format_seller_line(None) is None


def test_format_price_header_and_relative_time() -> None:
    drop = _format_price_header(new_price=21000, old_price=26000)
    assert "<s>26 000 ₽</s> ➔ <b>21 000 ₽</b>" in drop and "(−5 000 ₽ · −19%)" in drop
    assert _format_price_header(new_price=22500) == "<b>22 500 ₽</b>"
    now = datetime(2026, 11, 9, 12, 0, 0, tzinfo=timezone.utc)
    assert format_relative_time("2026-11-09T11:59:00Z", now_dt=now) == "только что"
    assert format_relative_time("2026-11-09T11:45:00Z", now_dt=now) == "15 мин назад"
    assert format_relative_time("2026-11-09T09:00:00Z", now_dt=now) == "3 ч назад"
    assert format_relative_time(None) is None and format_relative_time("bad") is None


def test_format_alert_caption_and_rich_html() -> None:
    c = format_alert_caption(
        "Sony <XM5>", 20000, location="СПб <Центр>", time_ago="5 мин",
        is_delivery=True, is_reserved=True, seller_line="👤 Иван",
        description="Хорошее состояние", search_name="Sony XM5",
    )
    assert "🚚 Есть доставка · 🔒 В брони" in c and "&lt;XM5&gt;" in c and "5 мин" in c
    assert "<blockquote>Хорошее состояние</blockquote>" in c
    assert "🎯 Поиск: «Sony XM5»" in c

    c_nodlv = format_alert_caption("Item", 1000, is_delivery=False, is_reserved=False)
    assert "🚚 Нет доставки" in c_nodlv and "В брони" not in c_nodlv

    c_unknown = format_alert_caption("Item", 1000)
    assert "🚚 Доставка не проверена" in c_unknown

    r = format_rich_html(
        "Sony <XM5>", 21000, old_price=26000, location="СПб", time_ago="5 мин",
        is_delivery=True, photo_ids=["p1", "p2"], seller_line="👤 Иван · ⭐ 5,0",
        description="Отличные наушники", search_name="Sony XM5", avito_id="4192837190",
    )
    assert '<tg-slideshow><img src="tg://photo?id=p1"/><img src="tg://photo?id=p2"/></tg-slideshow>' in r
    assert "<b>21 000 ₽</b> <s>26 000 ₽</s>" in r
    assert 'style="success"' in r and "−5 000 ₽ (−19%)" in r
    assert "<br/><br/>\n  📍 СПб" in r
    assert "<blockquote expandable>Отличные наушники</blockquote>" in r
    assert "🚚 Есть доставка<br/>\n  ⭐ 5,0" in r
    assert "👤 Иван" not in r
    assert "<p>🔗 https://www.avito.ru/4192837190</p>" in r
    assert "<p><br/></p>" in r
    assert "<footer>🎯 Sony XM5 · " in r

    r_res = format_rich_html("Item", 1000, is_delivery=False, is_reserved=True, location="Москва")
    assert 'style="danger">🔒 Забронировано</tg-button>' in r_res
    assert "🚚 Нет доставки" in r_res

    r_unknown = format_rich_html("Item", 1000)
    assert "🚚 Доставка не проверена" in r_unknown


def test_build_alert_keyboard() -> None:
    kb = build_alert_keyboard("8577782054")
    assert len(kb.inline_keyboard) == 1 and kb.inline_keyboard[0][0].url == "https://www.avito.ru/8577782054"
    assert build_alert_keyboard(None, url="https://a.ru").inline_keyboard[0][0].url == "https://a.ru"
