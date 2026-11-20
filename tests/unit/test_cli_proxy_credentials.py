import asyncio
import os
from types import SimpleNamespace

from typer.testing import CliRunner

import hunter.cli as cli
import hunter.cli_session as session_commands
from hunter.transport.proxy_credentials import ProxyCredentialStore


def test_import_proxy_credentials_from_descriptor(tmp_path, monkeypatch):
    secret = tmp_path / "secret.json"
    secret.write_text('{"username":"operator","password":"password-value"}')
    store = ProxyCredentialStore(tmp_path / "credentials")

    class Slots:
        async def get_slot(self, slot_id):
            return SimpleNamespace(endpoint="http://proxy:8080") if slot_id == "slot_a" else None

    bootstrap = SimpleNamespace(slot_repo=Slots(), proxy_credential_store=store)
    monkeypatch.setattr(session_commands, "_run_admin", lambda fn: asyncio.run(fn(bootstrap)))
    fd = os.open(secret, os.O_RDONLY)
    try:
        result = CliRunner().invoke(
            cli.app, ["slot", "import-proxy", "slot_a", "--proxy-fd", str(fd)]
        )
    finally:
        os.close(fd)
    assert result.exit_code == 0, result.output
    assert "password-value" not in result.output
    assert store.load("slot_a", "http://proxy:8080").password == "password-value"


def test_import_proxy_rejects_unknown_slot_without_printing_secrets(tmp_path, monkeypatch):
    secret = tmp_path / "secret.json"
    secret.write_text('{"username":"operator","password":"password-value"}')

    class Slots:
        async def get_slot(self, slot_id):
            return None

    bootstrap = SimpleNamespace(
        slot_repo=Slots(), proxy_credential_store=ProxyCredentialStore(tmp_path / "credentials")
    )
    monkeypatch.setattr(session_commands, "_run_admin", lambda fn: asyncio.run(fn(bootstrap)))
    fd = os.open(secret, os.O_RDONLY)
    try:
        result = CliRunner().invoke(
            cli.app, ["slot", "import-proxy", "missing", "--proxy-fd", str(fd)]
        )
    finally:
        os.close(fd)
    assert result.exit_code == 1
    assert "password-value" not in result.output
    assert not list((tmp_path / "credentials").glob("*.json"))
