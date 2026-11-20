from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import logging

import httpx
import pytest
from pydantic import SecretStr

from hunter.transport.telegram_relay import (
    TelegramRelayClient,
    TelegramRelayConfig,
    TelegramRelayReplay,
    TelegramRelayTimeout,
    TelegramRelayUnauthorized,
)

SECRET = "fake-relay-hmac-secret"
TOKEN = "123456:fake-telegram-token"


def config(timeout: float = 0.1) -> TelegramRelayConfig:
    return TelegramRelayConfig(
        "https://relay.example.test/api", frozenset({"relay.example.test"}),
        SecretStr(SECRET), timeout,
    )


@pytest.mark.parametrize("url,hosts", [
    ("http://relay.example.test", {"relay.example.test"}),
    ("https://other.example.test", {"relay.example.test"}),
    ("https://127.0.0.1", {"127.0.0.1"}),
    ("https://user:pass@relay.example.test", {"relay.example.test"}),
])
def test_endpoint_requires_https_and_exact_host_allowlist(url, hosts) -> None:
    with pytest.raises(ValueError, match="allowlist"):
        TelegramRelayConfig(url, frozenset(hosts), SecretStr(SECRET))


@pytest.mark.asyncio
async def test_signed_request_keeps_token_out_of_url_and_logs(caplog) -> None:
    seen = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        signed = request.headers["X-Relay-Timestamp"].encode() + b"\n"
        signed += request.headers["X-Relay-Nonce"].encode() + b"\n" + request.content
        expected = hmac.new(SECRET.encode(), signed, hashlib.sha256).hexdigest()
        assert hmac.compare_digest(request.headers["X-Relay-Signature"], expected)
        assert json.loads(request.content)["bot_token"] == TOKEN
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        relay = TelegramRelayClient(config(), client=http, clock=lambda: 1234,
                                    nonce_factory=lambda: "n-1")
        with caplog.at_level(logging.DEBUG):
            assert await relay.call("sendMessage", TOKEN, {"chat_id": 42}) == {"ok": True}
    assert len(seen) == 1 and seen[0].url.path == "/api/v1/telegram/rpc"
    assert TOKEN not in str(seen[0].url) and TOKEN not in caplog.text
    source = inspect.getsource(TelegramRelayClient.call)
    assert "logger." not in source and "bot_token}" not in source


@pytest.mark.parametrize("status,error", [
    (401, TelegramRelayUnauthorized), (403, TelegramRelayUnauthorized),
    (409, TelegramRelayReplay),
])
@pytest.mark.asyncio
async def test_auth_and_replay_rejections_do_not_echo_body(status, error) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=TOKEN)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(error) as caught:
            await TelegramRelayClient(config(), client=http).call("getUpdates", TOKEN)
    assert TOKEN not in str(caught.value)


@pytest.mark.asyncio
async def test_timeout_is_mapped_to_secret_free_error() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated timeout")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(TelegramRelayTimeout, match="timed out"):
            await TelegramRelayClient(config(), client=http).call("getUpdates", TOKEN)
