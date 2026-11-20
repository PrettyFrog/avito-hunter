"""Authenticated HTTPS transport contract for an external Telegram relay."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr


class TelegramRelayError(RuntimeError):
    """Base error that intentionally excludes credentials and response bodies."""


class TelegramRelayUnauthorized(TelegramRelayError):
    """The relay rejected its HMAC credentials."""


class TelegramRelayReplay(TelegramRelayError):
    """The relay rejected a duplicate or expired nonce/timestamp."""


class TelegramRelayTimeout(TelegramRelayError):
    """The relay did not answer within its configured timeout."""


class TelegramRelayUnavailable(TelegramRelayError):
    """The relay connection failed before an HTTP request could be forwarded."""


@dataclass(frozen=True, slots=True)
class TelegramRelayConfig:
    """Single relay endpoint and its server-owned allowlist/HMAC secret."""

    endpoint: str
    allowed_hosts: frozenset[str]
    hmac_secret: SecretStr
    timeout_seconds: float = 65.0

    def __post_init__(self) -> None:
        parsed = urlsplit(self.endpoint)
        host = (parsed.hostname or "").lower().rstrip(".")
        try:
            port = parsed.port
            ipaddress.ip_address(host)
            is_ip = True
        except ValueError:
            is_ip = False
            port = parsed.port
        allowed = {item.lower().rstrip(".") for item in self.allowed_hosts}
        if (
            parsed.scheme != "https"
            or not host
            or is_ip
            or host == "localhost"
            or host.endswith(".localhost")
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or port not in (None, 443)
            or host not in allowed
        ):
            raise ValueError("Telegram relay endpoint violates HTTPS host allowlist")
        if not self.hmac_secret.get_secret_value().strip():
            raise ValueError("Telegram relay HMAC secret is required")
        if self.timeout_seconds <= 0:
            raise ValueError("Telegram relay timeout must be positive")


class TelegramRelayClient:
    """POST Telegram RPC envelopes to a trusted relay using HMAC-SHA256.

    Protocol: POST /v1/telegram/rpc with JSON {method, bot_token, parameters};
    sign `timestamp + LF + nonce + LF + raw_body` as lowercase HMAC-SHA256 hex
    in X-Relay-Signature. The relay must enforce timestamp freshness, reject
    reused nonces with 409, and forward bot_token only to Telegram. The client
    never puts that token in a URL or diagnostic message.
    """

    def __init__(
        self,
        config: TelegramRelayConfig,
        *,
        secondary_config: TelegramRelayConfig | None = None,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.time,
        nonce_factory: Callable[[], str] = lambda: secrets.token_urlsafe(18),
    ) -> None:
        self.config = config
        self.secondary_config = secondary_config
        self.url = f"{config.endpoint.rstrip('/')}/v1/telegram/rpc"
        self.secondary_url = (
            f"{secondary_config.endpoint.rstrip('/')}/v1/telegram/rpc" if secondary_config else None
        )
        self._client = client or httpx.AsyncClient(
            timeout=config.timeout_seconds, follow_redirects=False, trust_env=False
        )
        self._owns_client = client is None
        self._clock = clock
        self._nonce_factory = nonce_factory

    async def call(self, method: str, bot_token: str, parameters: dict | None = None) -> dict:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", method):
            raise ValueError("Invalid Telegram method")
        if not bot_token or "\n" in bot_token or "\r" in bot_token:
            raise ValueError("Telegram bot token is required")
        try:
            return await self._call_endpoint(self.config, self.url, method, bot_token, parameters)
        except (TelegramRelayUnavailable, TelegramRelayUnauthorized, TelegramRelayReplay):
            if not self.secondary_config or not self.secondary_url:
                raise
        return await self._call_endpoint(
            self.secondary_config, self.secondary_url, method, bot_token, parameters
        )

    async def _call_endpoint(
        self,
        config: TelegramRelayConfig,
        url: str,
        method: str,
        bot_token: str,
        parameters: dict | None,
    ) -> dict:
        timestamp = str(int(self._clock()))
        nonce = self._nonce_factory()
        body = json.dumps(
            {"method": method, "bot_token": bot_token, "parameters": parameters or {}},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        signed = timestamp.encode() + b"\n" + nonce.encode() + b"\n" + body
        signature = hmac.new(
            config.hmac_secret.get_secret_value().encode("utf-8"),
            signed,
            hashlib.sha256,
        ).hexdigest()
        headers = {
            "Content-Type": "application/json",
            "X-Relay-Timestamp": timestamp,
            "X-Relay-Nonce": nonce,
            "X-Relay-Signature": signature,
        }
        try:
            async with asyncio.timeout(config.timeout_seconds):
                response = await self._client.post(
                    url, content=body, headers=headers, follow_redirects=False
                )
        except (httpx.ConnectError, httpx.ConnectTimeout):
            raise TelegramRelayUnavailable("Telegram relay connection failed") from None
        except (TimeoutError, httpx.TimeoutException):
            raise TelegramRelayTimeout("Telegram relay request timed out") from None
        except httpx.TransportError:
            raise TelegramRelayTimeout("Telegram relay transport failed after connect") from None
        if response.status_code in (401, 403):
            raise TelegramRelayUnauthorized("Telegram relay authentication failed")
        if response.status_code == 409:
            raise TelegramRelayReplay("Telegram relay rejected nonce or timestamp")
        if response.status_code >= 400:
            raise TelegramRelayError(f"Telegram relay returned HTTP {response.status_code}")
        try:
            result = response.json()
        except ValueError:
            raise TelegramRelayError("Telegram relay returned invalid JSON") from None
        if not isinstance(result, dict):
            raise TelegramRelayError("Telegram relay returned an invalid response")
        return result

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
