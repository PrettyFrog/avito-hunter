"""The public egress owner follows the existing day/night clock without daily quotas."""
from datetime import datetime
from unittest.mock import Mock

from hunter.application.egress_runtime import make_egress_gateway
from hunter.core.config import Settings
from hunter.domain.poll_policy import PollPolicy


def test_off_mode_has_no_gateway():
    assert make_egress_gateway(Settings(_env_file=None), Mock()) is None


def test_enforced_gateway_reads_existing_period_boundaries(monkeypatch):
    current = [datetime.fromisoformat("2026-11-05T00:59:59+03:00")]
    original = PollPolicy.is_day
    monkeypatch.setattr(PollPolicy, "is_day", lambda self: original(self, current[0]))
    settings = Settings(_env_file=None, egress_regulator={"mode": "enforce", "interval_sec": 7.5,
                        "night_interval_sec": 45, "admission_timeout_sec": 120})
    gateway = make_egress_gateway(settings, Mock())
    assert not gateway.budget.shared_upstreams
    assert gateway.budget._spacing_now() == 7.5
    current[0] = datetime.fromisoformat("2026-11-05T01:00:00+03:00")
    assert gateway.budget._spacing_now() == 45
