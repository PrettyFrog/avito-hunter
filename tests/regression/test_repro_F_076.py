"""Regression test for F-076: Configuration hierarchy precedence.

Environment variables (both with HUNTER_ prefix and bare names) must override
default values in config/settings.yaml.
"""

from __future__ import annotations

import os
import pytest

from hunter.core.config import get_settings


def test_env_vars_override_settings_yaml(monkeypatch: pytest.MonkeyPatch) -> None:
    # 1. Without env vars, baseline values come from settings.yaml
    get_settings.cache_clear()
    baseline = get_settings()
    assert baseline.notify_mode == "new"
    assert baseline.timezone == "Europe/Moscow"

    # 2. Set environment variables
    monkeypatch.setenv("HUNTER_NOTIFY_MODE", "shadow")
    monkeypatch.setenv("HUNTER_TIMEZONE", "Asia/Tokyo")
    monkeypatch.setenv("HUNTER_REQUEST_BUDGET__DAILY_CAP", "2500")

    get_settings.cache_clear()
    overridden = get_settings()

    assert overridden.notify_mode == "shadow", (
        f"Expected notify_mode='shadow', but got '{overridden.notify_mode}'"
    )
    assert overridden.timezone == "Asia/Tokyo", (
        f"Expected timezone='Asia/Tokyo', but got '{overridden.timezone}'"
    )
    assert overridden.request_budget.daily_cap == 2500, (
        f"Expected daily_cap=2500, but got {overridden.request_budget.daily_cap}"
    )


def test_bare_env_vars_override_settings_yaml(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTIFY_MODE", "shadow")
    get_settings.cache_clear()
    overridden = get_settings()
    assert overridden.notify_mode == "shadow"
