from __future__ import annotations

import asyncio
import json
import os
import sqlite3

import pytest
from typer.testing import CliRunner

from hunter.application.subscriptions import compute_query_id
from hunter.bootstrap import Bootstrap
from hunter.cli import app
from hunter.cli_parts.app_01 import check_public_v2_and_run
from hunter.core.config import Settings
from hunter.core.db import Database
from hunter.domain.query_url import validate_and_canonicalize_query_url
from hunter.domain.slots import SlotCapacityError
from hunter.storage.subscriptions import NoActiveSlotCapacityError
from hunter.transport.session_store import SessionStore


class FakeChannel:
    def __init__(self, settings):
        self.configured = bool(settings.telegram_bot_token and settings.telegram_chat_id)
        self.listen_args = None

    def enabled(self):
        return self.configured

    async def listen(self, db=None, subscription_service=None):
        self.listen_args = (db, subscription_service)


class FakeBootstrap:
    def __init__(self, settings):
        self.settings = settings
        self.senders = {}
        self._tasks = []
        self.db = object()
        self.subscription_service = object()
        self.shutdown_called = False

    async def startup(self):
        assert "telegram" in self.senders

    async def run_supervised(self, stop_event):
        await asyncio.sleep(0)
        return 0

    async def shutdown(self):
        self.shutdown_called = True


def _settings() -> Settings:
    settings = Settings(env="test")
    settings.public_v2.enabled = True
    settings.telegram_bot_token = "fake-token"
    settings.telegram_chat_id = "100"
    return settings


@pytest.mark.asyncio
async def test_public_v2_cli_fails_before_bootstrap_without_telegram_config():
    settings = _settings()
    settings.telegram_bot_token = None

    def unexpected_bootstrap(_settings):
        raise AssertionError("Bootstrap must not start without a delivery adapter")

    with pytest.raises(RuntimeError, match="requires a configured Telegram"):
        await check_public_v2_and_run(
            settings,
            channel_factory=FakeChannel,
            bootstrap_factory=unexpected_bootstrap,
            stop_event=asyncio.Event(),
        )


@pytest.mark.asyncio
async def test_public_v2_cli_wires_sender_and_listener_to_bootstrap():
    settings = _settings()
    channel = FakeChannel(settings)
    bootstrap = FakeBootstrap(settings)

    assert await check_public_v2_and_run(
        settings,
        channel_factory=lambda _settings: channel,
        bootstrap_factory=lambda _settings: bootstrap,
        stop_event=asyncio.Event(),
    )
    assert channel.listen_args == (bootstrap.db, bootstrap.subscription_service)
    assert bootstrap.shutdown_called


async def _fill_slot_and_reject_fifth() -> None:
    from hunter.core.config import get_settings

    settings = get_settings()
    bootstrap = Bootstrap(settings, db=Database(settings.db_path))
    await bootstrap.startup()
    try:
        await bootstrap.writer.submit(
            lambda conn: conn.execute(
                "INSERT INTO users (id, role, created_at) "
                "VALUES (8001, 'user', datetime('now'))"
            )
        )
        service = bootstrap.subscription_service
        service.auto_assign = True
        for index in range(4):
            await service.add_subscription(
                8001,
                f"https://avito.ru/moskva?q=capacity-{index}",
                50000,
            )

        fifth_url = "https://avito.ru/moskva?q=capacity-4"
        fifth_query = compute_query_id(
            validate_and_canonicalize_query_url(fifth_url, 50000)
        )
        with pytest.raises(SlotCapacityError):
            await bootstrap.slot_repo.admission_dry_run("isp-01", fifth_query)
        with pytest.raises(NoActiveSlotCapacityError):
            await service.add_subscription(8001, fifth_url, 50000)
        query_count = await (
            await bootstrap.db._conn.execute("SELECT count(*) FROM queries")
        ).fetchone()
        subscription_count = await (
            await bootstrap.db._conn.execute(
                "SELECT count(*) FROM subscriptions WHERE user_id = 8001"
            )
        ).fetchone()
        assert tuple(query_count) == (4,)
        assert tuple(subscription_count) == (4,)
    finally:
        await bootstrap.shutdown()


def test_operator_slot_cli_capacity_and_restart(tmp_path, monkeypatch):
    from hunter.core.config import get_settings

    monkeypatch.setenv("HUNTER_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    runner = CliRunner()
    endpoint = "http://127.0.0.1:8080"
    try:
        added = runner.invoke(
            app,
            ["slot", "add", "isp-01", "--endpoint", endpoint, "--role", "active"],
        )
        assert added.exit_code == 0, added.output

        asyncio.run(_fill_slot_and_reject_fifth())
        listed = runner.invoke(app, ["slot", "list"])
        health = runner.invoke(app, ["slot", "health", "isp-01"])
        rejected = runner.invoke(app, ["slot", "admission", "isp-01", "candidate-5"])
        assert listed.exit_code == 0 and "isp-01" in listed.output and "4/4" in listed.output
        assert health.exit_code == 0 and "READY" in health.output
        assert rejected.exit_code == 1 and "REJECTED" in rejected.output
        assert "proxy-secret" not in (
            added.output + listed.output + health.output + rejected.output
        )
        reserve = runner.invoke(
            app,
            [
                "slot",
                "add",
                "reserve-01",
                "--endpoint",
                "http://127.0.0.2:8080",
                "--role",
                "reserve",
            ],
        )
        disabled = runner.invoke(app, ["slot", "disable", "isp-01"])
        enabled = runner.invoke(app, ["slot", "enable", "isp-01"])
        retired = runner.invoke(app, ["slot", "retire", "isp-01"])
        retired_health = runner.invoke(app, ["slot", "health", "isp-01"])
        assert reserve.exit_code == 0, reserve.output
        assert disabled.exit_code == 0 and "DISABLED" in disabled.output
        assert enabled.exit_code == 0 and "WARMING" in enabled.output
        assert retired.exit_code == 0 and "RETIRED" in retired.output
        assert retired_health.exit_code == 0 and "health=DISABLED" in retired_health.output
        assert "reserve-secret" not in (
            reserve.output + disabled.output + enabled.output + retired.output
        )
    finally:
        get_settings.cache_clear()


def test_operator_session_import_from_fd_survives_store_reload(tmp_path, monkeypatch):
    from hunter.core.config import get_settings

    monkeypatch.setenv("HUNTER_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    runner = CliRunner()
    try:
        runner.invoke(
            app,
            ["slot", "add", "session-slot", "--endpoint", "http://127.0.0.3:8080"],
        )
        read_fd, write_fd = os.pipe()
        cookie_secret = "operator-session-secret"
        os.write(write_fd, json.dumps({"cookies": [{
            "name": "sid", "value": cookie_secret, "domain": ".avito.ru", "path": "/"
        }]}).encode())
        os.close(write_fd)
        imported = runner.invoke(
            app, ["slot", "import-session", "session-slot", "--session-fd", str(read_fd)]
        )
        os.fstat(read_fd)
        os.close(read_fd)
        assert imported.exit_code == 0, imported.output
        assert cookie_secret not in imported.output and "proxy-secret" not in imported.output
        payload = asyncio.run(SessionStore(tmp_path / "sessions").load_session("session-slot"))
        assert payload is not None and payload.cookies[0]["value"] == cookie_secret
        connection = sqlite3.connect(get_settings().db_path)
        version = connection.execute(
            "SELECT version FROM session_versions WHERE slot_id = 'session-slot'"
        ).fetchone()
        connection.close()
        assert version == (payload.metadata.version,)
        bad_read, bad_write = os.pipe()
        invalid_secret = "invalid-domain-cookie-secret"
        os.write(bad_write, json.dumps({"cookies": [{
            "name": "sid", "value": invalid_secret, "domain": "attacker.example"
        }]}).encode())
        os.close(bad_write)
        rejected = runner.invoke(
            app, ["slot", "import-session", "session-slot", "--session-fd", str(bad_read)]
        )
        os.close(bad_read)
        reloaded = asyncio.run(SessionStore(tmp_path / "sessions").load_session("session-slot"))
        assert rejected.exit_code == 1 and invalid_secret not in rejected.output
        assert reloaded is not None and reloaded.cookies[0]["value"] == cookie_secret
    finally:
        get_settings.cache_clear()
