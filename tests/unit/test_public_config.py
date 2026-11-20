"""Unit tests for PublicV2Config and parameter validation (S5 / T-498).

Covers:
- Default parameters: 30s day, 180s night, 01:00-07:00 MSK, capacity 4, reserve 2, 100 rub/IP.
- Validation and rejection of negative or out-of-range values.
- Incompatible grouping flags rejection.
- Independence of legacy and public settings.
- Zero secrets serialized in status dictionaries.
- Constructing PollPolicy from PublicV2Config.
"""

from __future__ import annotations

import pytest

from hunter.core.config import PublicV2Config, Settings
from hunter.domain.poll_policy import PollPolicy


def test_public_v2_config_defaults():
    cfg = PublicV2Config()
    assert cfg.enabled is False
    assert cfg.day_interval_sec == 30.0
    assert cfg.night_interval_sec == 180.0
    assert cfg.day_start_hour == 7
    assert cfg.night_start_hour == 1
    assert cfg.timezone == "Europe/Moscow"
    assert cfg.slot_capacity == 4
    assert cfg.reserve_slots == 2
    assert cfg.ip_monthly_rub == 100
    assert cfg.price_watch_enabled is False
    assert cfg.grouping_exact_enabled is True
    assert cfg.grouping_compatible_price_enabled is False
    assert cfg.grouping_broad_umbrella_enabled is False
    assert cfg.jitter_ratio == 0.25
    assert cfg.max_jitter_sec == 180.0


def test_invalid_intervals_and_hours_rejected():
    with pytest.raises(ValueError, match="Interval must be positive"):
        PublicV2Config(day_interval_sec=0)

    with pytest.raises(ValueError, match="Interval must be positive"):
        PublicV2Config(night_interval_sec=-5.0)

    with pytest.raises(ValueError, match="Hour must be between 0 and 23"):
        PublicV2Config(day_start_hour=24)

    with pytest.raises(ValueError, match="Hour must be between 0 and 23"):
        PublicV2Config(night_start_hour=-1)


def test_invalid_capacity_and_reserve_rejected():
    with pytest.raises(ValueError, match="Slot capacity must be between 1 and 4"):
        PublicV2Config(slot_capacity=0)

    with pytest.raises(ValueError, match="Slot capacity must be between 1 and 4"):
        PublicV2Config(slot_capacity=5)

    with pytest.raises(ValueError, match="Value must be non-negative"):
        PublicV2Config(reserve_slots=-1)

    with pytest.raises(ValueError, match="Value must be non-negative"):
        PublicV2Config(ip_monthly_rub=-10)


def test_invalid_timezone_rejected():
    with pytest.raises(ValueError, match="Invalid timezone"):
        PublicV2Config(timezone="Mars/Olympus_Mons")


def test_incompatible_grouping_flags_rejected():
    with pytest.raises(
        ValueError, match="Cannot enable broad umbrella grouping without exact grouping"
    ):
        PublicV2Config(grouping_exact_enabled=False, grouping_broad_umbrella_enabled=True)


def test_independent_legacy_and_public_settings():
    settings = Settings()
    # Modify legacy budget
    settings.budget["daily_cap"] = 1200
    # public_v2 is independent
    assert settings.public_v2.day_interval_sec == 30.0
    assert settings.public_v2.slot_capacity == 4

    # Custom public_v2 does not mutate legacy priority intervals
    custom_pub = PublicV2Config(day_interval_sec=45.0, slot_capacity=3)
    assert settings.priority_intervals["high"] == 15


def test_secrets_not_serialized_in_status_dict():
    cfg = PublicV2Config()
    status = cfg.to_status_dict()

    # Must contain essential operational config
    assert status["day_interval_sec"] == 30.0
    assert status["slot_capacity"] == 4
    assert status["reserve_slots"] == 2

    # Must NOT leak any secret fields
    serialized_text = str(status).lower()
    for secret_word in ("token", "key", "secret", "password", "auth", "bearer"):
        assert secret_word not in serialized_text


def test_poll_policy_from_config():
    cfg = PublicV2Config(day_interval_sec=25.0, night_interval_sec=150.0, slot_capacity=3)
    policy = PollPolicy.from_config(cfg)

    assert policy.day_interval_sec == 25.0
    assert policy.night_interval_sec == 150.0
    assert policy.max_queries_per_slot == 3
    assert policy.jitter_ratio == 0.25
    assert policy.max_jitter_sec == 180.0
