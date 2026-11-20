"""Pure delivery timing, retention and pause rules shared by storage and UI."""

from __future__ import annotations

import zoneinfo
from datetime import UTC, datetime, time, timedelta

PAUSE_HIERARCHY: dict[str, int] = {
    "owner_paused": 3,
    "bot_blocked": 2,
    "user_paused": 1,
}
DEFAULT_DELIVERY_MAX_AGE_SECONDS = 7 * 24 * 60 * 60


def _get_tz(tz_name: str) -> zoneinfo.ZoneInfo:
    try:
        return zoneinfo.ZoneInfo(tz_name)
    except Exception:
        return zoneinfo.ZoneInfo("UTC")


def is_in_quiet_hours(
    dt_utc: datetime, start_h: int | None, end_h: int | None, tz_name: str = "UTC"
) -> bool:
    if start_h is None or end_h is None or start_h == end_h:
        return False
    hour = dt_utc.astimezone(_get_tz(tz_name)).hour
    return (hour >= start_h or hour < end_h) if start_h > end_h else start_h <= hour < end_h


def calculate_available_at(
    created_at_utc: datetime,
    start_h: int | None,
    end_h: int | None,
    tz_name: str = "UTC",
) -> datetime:
    if not is_in_quiet_hours(created_at_utc, start_h, end_h, tz_name):
        return created_at_utc
    assert start_h is not None and end_h is not None
    tz = _get_tz(tz_name)
    local_dt = created_at_utc.astimezone(tz)
    target_date = local_dt.date()
    if start_h > end_h and local_dt.hour >= start_h:
        target_date += timedelta(days=1)
    end_local = datetime.combine(target_date, time(hour=end_h), tzinfo=tz)
    return end_local.astimezone(UTC)


def calculate_delivery_slo_latency(available_at_utc: datetime, sent_at_utc: datetime) -> float:
    return max(0.0, (sent_at_utc - available_at_utc).total_seconds())


def can_resume(pause_reason: str | None, actor_role: str) -> bool:
    if not pause_reason:
        return True
    if pause_reason in ("owner_paused", "bot_blocked"):
        return actor_role == "owner"
    return True


def is_delivery_expired(
    created_at_utc: datetime,
    now_utc: datetime,
    max_age_seconds: int = DEFAULT_DELIVERY_MAX_AGE_SECONDS,
) -> bool:
    return (now_utc - created_at_utc).total_seconds() > max_age_seconds
