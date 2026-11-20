from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hunter.core.models import NotificationEvent
from hunter.notify.base import format_event
from hunter.notify.helpers import format_publication_delay
from hunter.notify.telegram import format_user_alert


def test_format_publication_delay_exact_seconds():
    now = datetime(2026, 10, 21, 12, 0, 0, tzinfo=UTC)

    # 42 seconds ago
    pub_42s = (now - timedelta(seconds=42)).isoformat()
    assert format_publication_delay(pub_42s, ref_dt=now) == "42с"

    # 84 seconds ago (1m 24s)
    pub_84s = (now - timedelta(seconds=84)).isoformat()
    assert format_publication_delay(pub_84s, ref_dt=now) == "1м 24с"

    # 3605 seconds ago (1h 00m 05s)
    pub_1h = (now - timedelta(seconds=3605)).isoformat()
    assert format_publication_delay(pub_1h, ref_dt=now) == "1ч 00м 05с"

    # 90000 seconds ago (1d 1h 00m)
    pub_1d = (now - timedelta(seconds=90000)).isoformat()
    assert format_publication_delay(pub_1d, ref_dt=now) == "1д 1ч 00м"

    # None or empty
    assert format_publication_delay(None) is None
    assert format_publication_delay("") is None
    assert format_publication_delay("invalid-date") is None


def test_alerts_include_publication_delay():
    now = datetime(2026, 10, 21, 12, 0, 0, tzinfo=UTC)
    pub_84s = (now - timedelta(seconds=84)).isoformat()

    event = NotificationEvent(
        avito_id="1234567890",
        url="https://www.avito.ru/item/1234567890",
        title="Sony WH-1000XM5 Black",
        price=18000,
        max_price=22000,
        watchlist_id="sony_xm5",
        category="headphones",
        model_id="sony_wh1000xm5",
        verdict="send",
        reasons=["title_match"],
        city="Москва",
        published_at=pub_84s,
    )

    user_text = format_user_alert(event)
    assert "⏱ Задержка от публикации:" in user_text
    assert "18 000 ₽" in user_text
    assert "Sony WH-1000XM5 Black" in user_text

    admin_text = format_event(event)
    assert "⏱ Задержка от публикации:" in admin_text
    assert "18000 ₽" in admin_text


def test_missing_or_implausible_publication_timestamp_never_zero_latency():
    from hunter.application.metrics import EventLatencyProvenance

    obs = "2026-10-23T12:00:00+00:00"
    for bad in (None, "", "bad-date", "2026-10-23T12:10:00+00:00", "2020-01-01T00:00:00+00:00"):
        assert (
            EventLatencyProvenance(published_at=bad, observed_at=obs).compute_publication_lag()
            is None
        )
    assert (
        EventLatencyProvenance(
            published_at="2026-10-23T11:59:30+00:00", observed_at=obs
        ).compute_publication_lag()
        == 30.0
    )
