"""Rollout policy is explicit and captured when the runtime is constructed."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hunter.application.public_poll_runtime import PublicPollRuntime
from hunter.domain.poll_policy import PollPolicy


@pytest.mark.parametrize("value,expected", [(None, False), ("0", False), ("1", True)])
def test_identity_cleanup_rollout_switch(monkeypatch, value, expected):
    monkeypatch.delenv("HUNTER_IDENTITY_CLEANUP", raising=False)
    if value is not None:
        monkeypatch.setenv("HUNTER_IDENTITY_CLEANUP", value)
    runtime = PublicPollRuntime(None, SimpleNamespace(), AsyncMock(), PollPolicy())
    assert runtime.identity_cleanup is expected
    monkeypatch.setenv("HUNTER_IDENTITY_CLEANUP", "1" if not expected else "0")
    assert runtime.identity_cleanup is expected


def test_explicit_policy_overrides_environment(monkeypatch):
    monkeypatch.setenv("HUNTER_IDENTITY_CLEANUP", "1")
    runtime = PublicPollRuntime(None, SimpleNamespace(), AsyncMock(), PollPolicy(), identity_cleanup=False)
    assert runtime.identity_cleanup is False
