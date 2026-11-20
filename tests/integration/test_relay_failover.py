from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
import httpx
import pytest
from pydantic import SecretStr
from tests.integration.eligibility_fixture import attach_eligible_subscription

from hunter.application.cutover import CutoverMode
from hunter.application.dispatcher import SendResult
from hunter.bootstrap import Bootstrap
from hunter.core.config import Settings
from hunter.core.db import Database
from hunter.transport.telegram_relay import (
    TelegramRelayClient,
    TelegramRelayConfig,
    TelegramRelayTimeout,
    TelegramRelayUnavailable,
)


def _config(endpoint: str) -> TelegramRelayConfig:
    return TelegramRelayConfig(
        f"https://{endpoint}", frozenset({endpoint}), SecretStr("test-secret-not-real"), 0.1
    )


def _relay(handler) -> tuple[TelegramRelayClient, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
    return TelegramRelayClient(
        _config("primary.relay.test"),
        secondary_config=_config("secondary.relay.test"),
        client=client,
    ), client


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["connect", 401, 403, 409])
async def test_safe_primary_failure_uses_secondary_once(failure: str | int) -> None:
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        if request.url.host == "primary.relay.test":
            if failure == "connect":
                raise httpx.ConnectError("connection refused", request=request)
            return httpx.Response(int(failure))
        return httpx.Response(200, json={"ok": True, "result": "accepted"})

    relay, client = _relay(handler)
    try:
        result = await relay.call("sendMessage", "123456:test-token", {"chat_id": 7})
        assert result == {"ok": True, "result": "accepted"}
        assert hosts == ["primary.relay.test", "secondary.relay.test"]
    finally:
        await client.aclose()


async def _seed_delivery(app: Bootstrap, user_id: int) -> None:
    now = datetime.now(UTC).isoformat()

    async def seed(conn: aiosqlite.Connection) -> None:
        await conn.execute(
            "INSERT OR IGNORE INTO users (id, status, created_at) VALUES (?, 'active', ?)",
            (user_id, now),
        )
        await conn.execute(
            "INSERT INTO events (id, avito_id, revision, kind, observed_at) "
            "VALUES ('relay_event', 'relay_lot', 1, 'NEW_LISTING', ?)",
            (now,),
        )
        payload = await attach_eligible_subscription(conn, user_id, "relay_event", "relay_lot", now)
        await conn.execute(
            "INSERT INTO deliveries (id, event_id, user_id, channel, "
            "payload, status, available_at) "
            "VALUES ('relay_delivery', 'relay_event', ?, 'telegram', ?, 'pending', ?)",
            (user_id, payload, now),
        )

    await app.writer.submit(seed)


async def _new_app(
    tmp_path: Path, db_name: str, user_id: int, sender, *, seed: bool = True
) -> Bootstrap:
    settings = Settings(env="test")
    settings.public_v2.enabled = True
    app = Bootstrap(settings, db=Database(tmp_path / db_name), senders={"telegram": sender})
    await app.startup()
    await app.dispatcher.stop()
    await app.cutover_mgr.set_tenant_mode(user_id, CutoverMode.V2)
    if seed:
        await _seed_delivery(app, user_id)
    return app


@pytest.mark.asyncio
async def test_fault_lost_ack_stays_pending_without_secondary_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HUNTER_DATA_DIR", str(tmp_path))
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        raise httpx.ReadTimeout("ack lost after upstream acceptance", request=request)

    relay, client = _relay(handler)

    async def sender(_delivery) -> SendResult:
        try:
            await relay.call("sendMessage", "123456:test-token", {"chat_id": 7})
        except TelegramRelayTimeout:
            return SendResult("ambiguous", retry_after_seconds=1)
        return SendResult("sent", "unexpected")

    user_id = 84321
    app = await _new_app(tmp_path, "relay.db", user_id, sender)
    try:
        await app.dispatcher.dispatch_once()
        row = await (
            await app.db._conn.execute("SELECT status FROM deliveries WHERE id='relay_delivery'")
        ).fetchone()
        assert row[0] == "pending"
        assert hosts == ["primary.relay.test"]
        await app.shutdown()

        restarted = await _new_app(tmp_path, "relay.db", user_id, sender, seed=False)
        await restarted.writer.submit(
            lambda conn: conn.execute(
                "UPDATE deliveries SET available_at=? WHERE id='relay_delivery'",
                (datetime.now(UTC).isoformat(),),
            )
        )
        await restarted.dispatcher.dispatch_once()
        row = await (
            await restarted.db._conn.execute(
                "SELECT status, attempt FROM deliveries WHERE id='relay_delivery'"
            )
        ).fetchone()
        assert (row[0], row[1]) == ("pending", 2)
        assert hosts == ["primary.relay.test", "primary.relay.test"]
        await restarted.shutdown()
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_fault_both_relays_down_keep_outbox_pending_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HUNTER_DATA_DIR", str(tmp_path))
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        raise httpx.ConnectError("connection refused", request=request)

    relay, client = _relay(handler)

    async def sender(_delivery) -> SendResult:
        try:
            await relay.call("sendMessage", "123456:test-token", {"chat_id": 7})
        except TelegramRelayUnavailable:
            return SendResult("retry", retry_after_seconds=1)
        return SendResult("sent", "unexpected")

    user_id = 84322
    app = await _new_app(tmp_path, "both-relays-down.db", user_id, sender)
    try:
        await app.dispatcher.dispatch_once()
        await app.shutdown()
        restarted = await _new_app(tmp_path, "both-relays-down.db", user_id, sender, seed=False)
        await restarted.writer.submit(
            lambda conn: conn.execute(
                "UPDATE deliveries SET available_at=? WHERE id='relay_delivery'",
                (datetime.now(UTC).isoformat(),),
            )
        )
        await restarted.dispatcher.dispatch_once()
        row = await (
            await restarted.db._conn.execute(
                "SELECT status, attempt FROM deliveries WHERE id='relay_delivery'"
            )
        ).fetchone()
        assert (row[0], row[1]) == ("pending", 2)
        assert hosts == [
            "primary.relay.test",
            "secondary.relay.test",
            "primary.relay.test",
            "secondary.relay.test",
        ]
        await restarted.shutdown()
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_telegram_429_is_not_a_relay_failover_signal() -> None:
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        return httpx.Response(
            200, json={"ok": False, "error_code": 429, "parameters": {"retry_after": 17}}
        )

    relay, client = _relay(handler)
    try:
        result = await relay.call("sendMessage", "123456:test-token", {"chat_id": 7})
        assert result["parameters"]["retry_after"] == 17
        assert hosts == ["primary.relay.test"]
    finally:
        await client.aclose()
