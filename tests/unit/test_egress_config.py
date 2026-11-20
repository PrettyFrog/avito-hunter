"""Public pacing config has explicit intervals, physical groups and no daily quota."""
import pytest
from pydantic import ValidationError


def test_default_is_off_and_enforce_needs_explicit_positive_interval():
    from hunter.core.egress_config import EgressRegulatorConfig
    assert EgressRegulatorConfig().mode == "off"
    with pytest.raises(ValidationError):
        EgressRegulatorConfig(mode="enforce")
    config = EgressRegulatorConfig(mode="enforce", interval_sec=7.5, night_interval_sec=45)
    assert config.interval_sec == 7.5 and config.night_interval_sec == 45
    assert config.spacing_for(is_day=True) == 7.5
    assert config.spacing_for(is_day=False) == 45
    with pytest.raises(ValidationError):
        EgressRegulatorConfig(daily_cap=100)


@pytest.mark.parametrize("kwargs", [{"interval_sec": float("inf")}, {"interval_sec": -1},
    {"night_interval_sec": float("nan")}, {"max_active": 0}, {"max_waiters": 0},
    {"admission_timeout_sec": 0}, {"mode": "enforce", "interval_sec": 10, "admission_timeout_sec": 5}])
def test_invalid_or_nonrepresentable_config_is_rejected(kwargs):
    from hunter.core.egress_config import EgressRegulatorConfig
    with pytest.raises(ValidationError):
        EgressRegulatorConfig(**kwargs)


def test_unknown_proxies_coalesce_with_direct_and_explicit_groups_share():
    from hunter.core.egress_config import EgressRegulatorConfig
    config = EgressRegulatorConfig(groups={"a": "isp-1", "b": "isp-1", "c": "isp-2"})
    assert config.group_for("any", "direct") == config.group_for("unknown", "http://proxy.test:8080") == "default"
    assert config.group_for("a", "http://proxy.test:8080") == config.group_for("b", "http://proxy.test:8081")
    assert config.group_for("c", "http://proxy.test:8082") == "isp-2"
    with pytest.raises(ValueError):
        config.group_for("a", "direct")


def test_error_does_not_echo_accidental_proxy_credentials():
    from hunter.core.egress_config import EgressRegulatorConfig
    with pytest.raises(ValidationError) as caught:
        EgressRegulatorConfig(groups={"slot": "http://synthetic-user:synthetic-secret@proxy.test"})
    assert "synthetic-secret" not in str(caught.value)


def test_settings_nested_environment_uses_new_regulator_without_touching_poll_policy(monkeypatch):
    from hunter.core.config import Settings
    monkeypatch.setenv("HUNTER_EGRESS_REGULATOR__MODE", "enforce")
    monkeypatch.setenv("HUNTER_EGRESS_REGULATOR__INTERVAL_SEC", "7.5")
    settings = Settings(_env_file=None, public_v2={"day_interval_sec": 11.89,
                        "night_interval_sec": 35.67, "jitter_ratio": 0.25})
    assert settings.egress_regulator.mode == "enforce"
    assert settings.egress_regulator.interval_sec == 7.5
    assert settings.public_v2.day_interval_sec == 11.89
    assert settings.public_v2.night_interval_sec == 35.67
    assert settings.public_v2.jitter_ratio == 0.25


def test_nested_settings_error_hides_accidental_credentials(monkeypatch):
    from hunter.core.config import Settings
    monkeypatch.setenv("HUNTER_EGRESS_REGULATOR__GROUPS",
                       '{"slot":"http://synthetic-user:synthetic-secret@proxy.test"}')
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None)
    assert "synthetic-secret" not in str(caught.value)


def test_max_cooldown_sec_default_and_validation():
    from hunter.core.egress_config import EgressRegulatorConfig
    config = EgressRegulatorConfig()
    assert config.max_cooldown_sec == 902.58
    custom = EgressRegulatorConfig(max_cooldown_sec=500.0)
    assert custom.max_cooldown_sec == 500.0

