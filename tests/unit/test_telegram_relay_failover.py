from __future__ import annotations

import httpx
from pydantic import SecretStr
import pytest

from hunter.transport.telegram_relay import (
    TelegramRelayClient,
    TelegramRelayConfig,
    TelegramRelayReplay,
    TelegramRelayTimeout,
    TelegramRelayUnauthorized,
    TelegramRelayUnavailable,
)


def _make_config(host: str) -> TelegramRelayConfig:
    return TelegramRelayConfig(
        endpoint=f"https://{host}",
        allowed_hosts=frozenset({host}),
        hmac_secret=SecretStr("fake-secret-key-for-test-32chars"),
        timeout_seconds=5.0,
    )


def _make_client(handler) -> tuple[TelegramRelayClient, httpx.AsyncClient]:
    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport, trust_env=False)
    relay = TelegramRelayClient(
        config=_make_config("relay-primary.example.com"),
        secondary_config=_make_config("relay-secondary.example.com"),
        client=http_client,
    )
    return relay, http_client


@pytest.mark.asyncio
async def test_primary_healthy_does_not_call_secondary() -> None:
    requested_hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_hosts.append(request.url.host)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 101}})

    relay, http_client = _make_client(handler)
    try:
        res = await relay.call("sendMessage", "123456:FAKE_TOKEN", {"chat_id": 42, "text": "hello"})
        assert res["ok"] is True
        assert requested_hosts == ["relay-primary.example.com"]
    finally:
        await http_client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 403, 409])
async def test_primary_recoverable_status_fails_over_to_secondary(status_code: int) -> None:
    requested_hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_hosts.append(request.url.host)
        if request.url.host == "relay-primary.example.com":
            return httpx.Response(status_code)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 102}})

    relay, http_client = _make_client(handler)
    try:
        res = await relay.call("sendMessage", "123456:FAKE_TOKEN", {"chat_id": 42})
        assert res["ok"] is True
        assert requested_hosts == ["relay-primary.example.com", "relay-secondary.example.com"]
    finally:
        await http_client.aclose()


@pytest.mark.asyncio
async def test_primary_connection_error_fails_over_to_secondary() -> None:
    requested_hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_hosts.append(request.url.host)
        if request.url.host == "relay-primary.example.com":
            raise httpx.ConnectError("Connection refused by primary", request=request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 103}})

    relay, http_client = _make_client(handler)
    try:
        res = await relay.call("sendMessage", "123456:FAKE_TOKEN", {"chat_id": 42})
        assert res["ok"] is True
        assert requested_hosts == ["relay-primary.example.com", "relay-secondary.example.com"]
    finally:
        await http_client.aclose()


@pytest.mark.asyncio
async def test_primary_timeout_does_not_failover_preventing_duplicate() -> None:
    requested_hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_hosts.append(request.url.host)
        if request.url.host == "relay-primary.example.com":
            raise httpx.ReadTimeout("Upstream read timed out", request=request)
        return httpx.Response(200, json={"ok": True})

    relay, http_client = _make_client(handler)
    try:
        with pytest.raises(TelegramRelayTimeout):
            await relay.call("sendMessage", "123456:FAKE_TOKEN", {"chat_id": 42})
        assert requested_hosts == ["relay-primary.example.com"]
    finally:
        await http_client.aclose()


@pytest.mark.asyncio
async def test_both_relays_down_raises_unavailable() -> None:
    requested_hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_hosts.append(request.url.host)
        raise httpx.ConnectError("Connect failure", request=request)

    relay, http_client = _make_client(handler)
    try:
        with pytest.raises(TelegramRelayUnavailable):
            await relay.call("sendMessage", "123456:FAKE_TOKEN", {"chat_id": 42})
        assert requested_hosts == ["relay-primary.example.com", "relay-secondary.example.com"]
    finally:
        await http_client.aclose()


def test_relay_config_rejects_insecure_or_disallowed_hosts() -> None:
    # Scheme must be https
    with pytest.raises(ValueError, match="HTTPS"):
        TelegramRelayConfig(
            endpoint="http://relay.example.com",
            allowed_hosts=frozenset({"relay.example.com"}),
            hmac_secret=SecretStr("secret"),
        )

    # Disallowed host
    with pytest.raises(ValueError, match="allowlist"):
        TelegramRelayConfig(
            endpoint="https://attacker.example.com",
            allowed_hosts=frozenset({"relay.example.com"}),
            hmac_secret=SecretStr("secret"),
        )

    # Embedded credentials rejected
    with pytest.raises(ValueError, match="allowlist"):
        TelegramRelayConfig(
            endpoint="https://user:pass@relay.example.com",
            allowed_hosts=frozenset({"relay.example.com"}),
            hmac_secret=SecretStr("secret"),
        )
