"""Unit tests for delivery policy, quiet hours deferral, and pause hierarchy (T-488)."""

from datetime import UTC, datetime, timedelta

from hunter.application.delivery_policy import (
    calculate_available_at,
    calculate_delivery_slo_latency,
    can_resume,
    is_delivery_expired,
    is_in_quiet_hours,
)


def test_quiet_hours_midnight_boundary_and_deferral() -> None:
    # User in Europe/Moscow (UTC+3) with quiet hours 23:00 -> 08:00
    tz = "Europe/Moscow"

    # 1. 23:30 Moscow (20:30 UTC) -> inside quiet hours
    t_night = datetime(2026, 10, 23, 20, 30, tzinfo=UTC)
    assert is_in_quiet_hours(t_night, 23, 8, tz_name=tz) is True

    # Deferred to 08:00 Moscow next day (05:00 UTC)
    avail_night = calculate_available_at(t_night, 23, 8, tz_name=tz)
    assert avail_night == datetime(2026, 10, 24, 5, 0, tzinfo=UTC)

    # 2. 07:15 Moscow morning (04:15 UTC) -> inside quiet hours
    t_morning = datetime(2026, 10, 24, 4, 15, tzinfo=UTC)
    assert is_in_quiet_hours(t_morning, 23, 8, tz_name=tz) is True
    avail_morn = calculate_available_at(t_morning, 23, 8, tz_name=tz)
    assert avail_morn == datetime(2026, 10, 24, 5, 0, tzinfo=UTC)

    # 3. 14:00 Moscow afternoon (11:00 UTC) -> not quiet hours
    t_day = datetime(2026, 10, 24, 11, 0, tzinfo=UTC)
    assert is_in_quiet_hours(t_day, 23, 8, tz_name=tz) is False
    assert calculate_available_at(t_day, 23, 8, tz_name=tz) == t_day


def test_quiet_hours_daytime_interval_no_wrap() -> None:
    # Interval 1:00 -> 6:00 UTC
    t_in = datetime(2026, 10, 23, 3, 0, tzinfo=UTC)
    assert is_in_quiet_hours(t_in, 1, 6) is True
    avail = calculate_available_at(t_in, 1, 6)
    assert avail == datetime(2026, 10, 23, 6, 0, tzinfo=UTC)


def test_deferred_delivery_excluded_from_instant_slo() -> None:
    # Delivery available at 05:00:00 UTC (after quiet hours)
    avail = datetime(2026, 10, 24, 5, 0, 0, tzinfo=UTC)
    # Delivered at 05:00:12 UTC (12s dispatch latency)
    sent = datetime(2026, 10, 24, 5, 0, 12, tzinfo=UTC)

    slo_latency = calculate_delivery_slo_latency(avail, sent)
    # SLO latency is 12s, not penalized by overnight deferral
    assert slo_latency == 12.0
    assert slo_latency <= 30.0


def test_pause_hierarchy_resume_permissions() -> None:
    # Owner paused can only be resumed by owner
    assert can_resume("owner_paused", "user") is False
    assert can_resume("owner_paused", "owner") is True

    # Bot blocked can only be resumed by owner
    assert can_resume("bot_blocked", "user") is False
    assert can_resume("bot_blocked", "owner") is True

    # User paused can be resumed by user or owner
    assert can_resume("user_paused", "user") is True
    assert can_resume("user_paused", "owner") is True
    assert can_resume(None, "user") is True


def test_delivery_expiration_policy() -> None:
    t_created = datetime(2026, 10, 23, 10, 0, tzinfo=UTC)

    # 1 hour later -> not expired
    t_1h = t_created + timedelta(hours=1)
    assert is_delivery_expired(t_created, t_1h) is False

    # 25 hours later -> retained under the seven-day public outbox policy
    t_25h = t_created + timedelta(hours=25)
    assert is_delivery_expired(t_created, t_25h) is False

    t_7d = t_created + timedelta(days=7)
    assert is_delivery_expired(t_created, t_7d) is False
    t_7d_plus_one_second = t_7d + timedelta(seconds=1)
    assert is_delivery_expired(t_created, t_7d_plus_one_second) is True
