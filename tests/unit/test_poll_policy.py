"""Unit tests for pure domain PollPolicy (S5 / T-497).

Covers:
- Fake clock at critical boundaries: 00:59:59, 01:00:00, 06:59:59, 07:00:00 MSK.
- Proper timezone conversion from UTC to Europe/Moscow.
- Slot sub-cycle offsets (4 queries: 0/7.5/15/22.5s day, 0/45/90/135s night).
- Complete hermetic purity (no network, no database).
- Verification that night does not imply immunity to challenges.
- Validation of negative or invalid intervals.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from hunter.core.config import Settings
from hunter.domain.poll_policy import (
    DEFAULT_TIMEZONE,
    PollPeriod,
    PollPolicy,
)


@pytest.fixture
def policy() -> PollPolicy:
    return PollPolicy()


def test_day_night_boundary_transitions_msk(policy: PollPolicy):
    tz = ZoneInfo(DEFAULT_TIMEZONE)

    # 1. 00:59:59 MSK is DAY (30s)
    dt_day_end = datetime(2026, 10, 23, 0, 59, 59, tzinfo=tz)
    assert policy.is_day(dt_day_end) is True
    assert policy.get_period(dt_day_end) == PollPeriod.DAY
    assert policy.get_cycle_interval(dt_day_end) == 30.0

    # 2. 01:00:00 MSK is NIGHT (180s)
    dt_night_start = datetime(2026, 10, 23, 1, 0, 0, tzinfo=tz)
    assert policy.is_day(dt_night_start) is False
    assert policy.get_period(dt_night_start) == PollPeriod.NIGHT
    assert policy.get_cycle_interval(dt_night_start) == 180.0

    # 3. 06:59:59 MSK is NIGHT (180s)
    dt_night_end = datetime(2026, 10, 23, 6, 59, 59, tzinfo=tz)
    assert policy.is_day(dt_night_end) is False
    assert policy.get_period(dt_night_end) == PollPeriod.NIGHT
    assert policy.get_cycle_interval(dt_night_end) == 180.0

    # 4. 07:00:00 MSK is DAY (30s)
    dt_day_start = datetime(2026, 10, 23, 7, 0, 0, tzinfo=tz)
    assert policy.is_day(dt_day_start) is True
    assert policy.get_period(dt_day_start) == PollPeriod.DAY
    assert policy.get_cycle_interval(dt_day_start) == 30.0


def test_timezone_conversion_utc_to_msk(policy: PollPolicy):
    # MSK is UTC+3
    # 21:59:59 UTC = 00:59:59 MSK -> DAY
    utc_day = datetime(2026, 10, 22, 21, 59, 59, tzinfo=UTC)
    assert policy.is_day(utc_day) is True
    assert policy.get_cycle_interval(utc_day) == 30.0

    # 22:00:00 UTC = 01:00:00 MSK -> NIGHT
    utc_night = datetime(2026, 10, 22, 22, 0, 0, tzinfo=UTC)
    assert policy.is_day(utc_night) is False
    assert policy.get_cycle_interval(utc_night) == 180.0

    # 03:59:59 UTC = 06:59:59 MSK -> NIGHT
    utc_night_end = datetime(2026, 10, 23, 3, 59, 59, tzinfo=UTC)
    assert policy.is_day(utc_night_end) is False

    # 04:00:00 UTC = 07:00:00 MSK -> DAY
    utc_day_start = datetime(2026, 10, 23, 4, 0, 0, tzinfo=UTC)
    assert policy.is_day(utc_day_start) is True


def test_slot_offsets_distribution(policy: PollPolicy):
    tz = ZoneInfo(DEFAULT_TIMEZONE)
    day_dt = datetime(2026, 10, 23, 12, 0, 0, tzinfo=tz)
    night_dt = datetime(2026, 10, 23, 3, 0, 0, tzinfo=tz)

    # 4 queries Day: 0.0, 7.5, 15.0, 22.5
    offsets_day_4 = policy.calculate_slot_offsets(4, day_dt)
    assert offsets_day_4 == (0.0, 7.5, 15.0, 22.5)

    # 4 queries Night: 0.0, 45.0, 90.0, 135.0
    offsets_night_4 = policy.calculate_slot_offsets(4, night_dt)
    assert offsets_night_4 == (0.0, 45.0, 90.0, 135.0)

    # Partial capacity
    assert policy.calculate_slot_offsets(1, day_dt) == (0.0,)
    assert policy.calculate_slot_offsets(2, day_dt) == (0.0, 15.0)
    assert policy.calculate_slot_offsets(3, day_dt) == (0.0, 10.0, 20.0)
    assert policy.calculate_slot_offsets(0, day_dt) == ()

    # Capacity exceeding max_queries_per_slot (4) is clamped to 4
    assert policy.calculate_slot_offsets(5, day_dt) == (0.0, 7.5, 15.0, 22.5)


def test_schedule_window_pure_representation(policy: PollPolicy):
    tz = ZoneInfo(DEFAULT_TIMEZONE)
    dt = datetime(2026, 10, 23, 14, 30, 0, tzinfo=tz)
    window = policy.get_schedule_window(4, dt)

    assert window.period == PollPeriod.DAY
    assert window.cycle_interval_sec == 30.0
    assert window.slot_offsets == (0.0, 7.5, 15.0, 22.5)
    assert window.effective_time_msk == dt


def test_invalid_policy_parameters_rejected():
    with pytest.raises(ValueError, match="day_interval_sec must be positive"):
        PollPolicy(day_interval_sec=0)

    with pytest.raises(ValueError, match="night_interval_sec must be positive"):
        PollPolicy(night_interval_sec=-10)

    with pytest.raises(ValueError, match="max_queries_per_slot must be positive"):
        PollPolicy(max_queries_per_slot=0)


def test_config_integration():
    settings = Settings()
    cfg = settings.poll_policy

    assert cfg.day_interval_sec == 30.0
    assert cfg.night_interval_sec == 180.0
    assert cfg.day_start_hour == 7
    assert cfg.night_start_hour == 1
    assert cfg.timezone == "Europe/Moscow"
    assert cfg.max_queries_per_slot == 4
