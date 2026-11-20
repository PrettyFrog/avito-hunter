from __future__ import annotations

from pathlib import Path
import yaml
import pytest

from hunter.domain.slots import SlotRole, validate_proxy_endpoint, validate_slot_id


def test_slots_example_config_schema_and_security() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    config_file = repo_root / "config" / "slots.example.yaml"
    assert config_file.is_file(), f"Missing {config_file}"

    raw_text = config_file.read_text(encoding="utf-8")
    assert "password" not in raw_text.lower()
    assert "token" not in raw_text.lower() or "никаких" in raw_text.lower()
    assert "@" not in raw_text  # no credentials in endpoints

    data = yaml.safe_load(raw_text)
    assert isinstance(data, dict)
    assert "slots" in data
    slots = data["slots"]
    assert isinstance(slots, list) and len(slots) >= 2

    roles = set()
    for slot in slots:
        assert "slot_id" in slot
        assert "endpoint" in slot
        assert "role" in slot

        # Domain validations
        validate_slot_id(slot["slot_id"])
        validate_proxy_endpoint(slot["endpoint"])
        role = SlotRole(slot["role"])
        roles.add(role)

        assert slot.get("max_queries", 4) <= 4

    assert SlotRole.ACTIVE in roles
    assert SlotRole.RESERVE in roles


def test_systemd_service_unit_security_directives() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    service_file = repo_root / "ops" / "avito-hunter.service"
    assert service_file.is_file(), f"Missing {service_file}"

    content = service_file.read_text(encoding="utf-8")
    assert "User=avito-hunter" in content
    assert "NoNewPrivileges=true" in content
    assert "ProtectSystem=strict" in content
    assert "ProtectHome=true" in content
    assert "PrivateTmp=true" in content
    assert "Restart=on-failure" in content
