"""Unit tests for Qrator browserless handshake service (Phase 1)."""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from unittest.mock import AsyncMock

import pytest

from hunter.transport.metered_http import HttpDeferred
from hunter.transport.qrator_handshake import (
    HandshakeOutcome,
    QratorHandshakeResult,
    _extract_ft_token,
    is_ft_expired,
    perform_qrator_handshake,
)
from hunter.transport.service_singleflight import ServiceSingleFlight


class FakeResponse:
    def __init__(self, status_code: int = 200, text: str = "", headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}
        self.cookies: dict[str, str] = {}


class FakeClient:
    def __init__(self, ft_response: FakeResponse, pixel_response: FakeResponse | None = None) -> None:
        self.ft_response = ft_response
        self.pixel_response = pixel_response or FakeResponse(200, "")
        self.post_calls: list[dict[str, Any]] = []
        self.get_calls: list[dict[str, Any]] = []
        import httpx
        self.cookies = httpx.Cookies()

    async def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.post_calls.append({"url": url, **kwargs})
        return self.ft_response

    async def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.get_calls.append({"url": url, **kwargs})
        return self.pixel_response


@pytest.mark.asyncio
async def test_handshake_success() -> None:
    token_str = "4b42377b-e10c-482d-8b06-4df0da00ad20:dGhpcy1pcy1hLXZhbGlkLXFyYXRvci1mdC10b2tlbi1mb3ItdGVzdGluZw=="
    client = FakeClient(ft_response=FakeResponse(200, json.dumps(token_str)))

    result = await perform_qrator_handshake(
        client=client,
        slot_id="slot-test-1",
        identity="user_a",
        generation=0,
    )

    assert result.outcome == HandshakeOutcome.SUCCESS
    assert result.is_success is True
    assert result.f_val is not None and result.f_val.startswith("5.")
    assert result.ft_val == token_str
    assert result.acquired_at is not None and result.acquired_at > 0

    # Cookie format assertions
    cookie_dict = {c["name"]: c["value"] for c in result.cookies}
    assert cookie_dict["f"] == result.f_val
    assert cookie_dict["ft"] == f'"{token_str}"'  # Quoted!

    # Wire verification
    assert len(client.post_calls) == 1
    post_req = client.post_calls[0]
    assert post_req["url"] == "https://www.avito.ru/web/2/ft"
    assert "multipart/form-data;" in post_req["headers"]["Content-Type"]
    assert "boundary=" in post_req["headers"]["Content-Type"]
    assert b'name="f"' in post_req["data"]
    assert b'name="s"' in post_req["data"]

    # Pixel ping
    assert len(client.get_calls) == 1
    assert "https://www.avito.ru/web/1/u?" in client.get_calls[0]["url"]


@pytest.mark.asyncio
async def test_handshake_rate_limited_429() -> None:
    client = FakeClient(ft_response=FakeResponse(429, "Rate limited", headers={"Retry-After": "45"}))

    result = await perform_qrator_handshake(
        client=client,
        slot_id="slot-test-1",
        identity="user_a",
        generation=0,
    )

    assert result.outcome == HandshakeOutcome.RATE_LIMITED
    assert result.is_success is False
    assert result.retry_after_sec == 45.0
    assert len(client.get_calls) == 0  # No pixel ping on failure


@pytest.mark.asyncio
async def test_handshake_blocked_403() -> None:
    client = FakeClient(ft_response=FakeResponse(403, "Forbidden"))

    result = await perform_qrator_handshake(
        client=client,
        slot_id="slot-test-1",
        identity="user_a",
        generation=0,
    )

    assert result.outcome == HandshakeOutcome.BLOCKED
    assert result.is_success is False
    assert len(client.get_calls) == 0


@pytest.mark.asyncio
async def test_handshake_network_error_graceful_fallback() -> None:
    client = FakeClient(ft_response=FakeResponse(500, "Server Error"))
    # Simulate network exception
    client.post = AsyncMock(side_effect=Exception("Connection timed out"))

    result = await perform_qrator_handshake(
        client=client,
        slot_id="slot-test-1",
        identity="user_a",
        generation=0,
    )

    assert result.outcome == HandshakeOutcome.ERROR
    assert result.is_success is False
    assert "Connection timed out" in result.details


@pytest.mark.asyncio
async def test_handshake_single_flight_coalescing() -> None:
    token_str = "4b42377b-e10c-482d-8b06-4df0da00ad20:dGhpcy1pcy1hLXZhbGlkLXFyYXRvci1mdC10b2tlbg=="
    client = FakeClient(ft_response=FakeResponse(200, json.dumps(token_str)))
    sf = ServiceSingleFlight()

    # Launch 5 concurrent handshakes for the exact same slot and identity
    results = await asyncio.gather(*[
        perform_qrator_handshake(
            client=client,
            slot_id="slot-test-1",
            identity="user_a",
            generation=1,
            single_flight=sf,
        )
        for _ in range(5)
    ])

    assert all(r.is_success for r in results)
    assert len(client.post_calls) == 1  # Exactly ONE HTTP POST executed!


def test_is_ft_expired() -> None:
    assert is_ft_expired(None) is True
    assert is_ft_expired(0.0) is True
    assert is_ft_expired(time.time() - 100.0, assumed_ttl_sec=64800.0) is False
    assert is_ft_expired(time.time() - 70000.0, assumed_ttl_sec=64800.0) is True


@pytest.mark.asyncio
async def test_enrich_recovery_cookies_success() -> None:
    token_str = "4b42377b-e10c-482d-8b06-4df0da00ad20:dGhpcy1pcy1hLXZhbGlkLXFyYXRvci1mdC10b2tlbg=="
    client = FakeClient(ft_response=FakeResponse(200, json.dumps(token_str)))
    base_cookies = [{"name": "_adcc", "value": "123", "domain": ".avito.ru", "path": "/"}]

    from hunter.transport.qrator_handshake import enrich_recovery_cookies
    enriched = await enrich_recovery_cookies(client, "slot-1", "user_a", base_cookies)

    cookie_dict = {c["name"]: c["value"] for c in enriched}
    assert cookie_dict["_adcc"] == "123"
    assert "f" in cookie_dict and cookie_dict["f"].startswith("5.")
    assert cookie_dict["ft"] == f'"{token_str}"'


@pytest.mark.asyncio
async def test_enrich_recovery_cookies_fallback_on_error() -> None:
    client = FakeClient(ft_response=FakeResponse(403, "Forbidden"))
    base_cookies = [{"name": "_adcc", "value": "123", "domain": ".avito.ru", "path": "/"}]

    from hunter.transport.qrator_handshake import enrich_recovery_cookies
    enriched = await enrich_recovery_cookies(client, "slot-1", "user_a", base_cookies)
    assert enriched == base_cookies


@pytest.mark.asyncio
async def test_session_warmup_acquires_ft_token() -> None:
    token_str = "4b42377b-e10c-482d-8b06-4df0da00ad20:dGhpcy1pcy1hLXZhbGlkLXFyYXRvci1mdC10b2tlbg=="
    catalog_html = '<div data-marker="catalog-serp"><div data-marker="item">item1</div></div>'

    class WarmupClient:
        def __init__(self) -> None:
            self.cookies: dict[str, str] = {}
            self.headers = {"User-Agent": "TestUA/1.0"}

        async def get(self, url: str, **kwargs: Any) -> FakeResponse:
            if "/web/1/u?" in url:
                return FakeResponse(200, "")
            return FakeResponse(200, catalog_html)

        async def post(self, url: str, **kwargs: Any) -> FakeResponse:
            if "/web/2/ft" in url:
                return FakeResponse(200, json.dumps(token_str))
            return FakeResponse(200, "{}")

    w_client = WarmupClient()
    from hunter.transport.session_warmup import perform_session_warmup, WarmupOutcome
    res = await perform_session_warmup(w_client, "https://www.avito.ru/catalog")

    assert res.outcome == WarmupOutcome.READY
    assert res.is_ready is True
    assert "f" in w_client.cookies
    assert w_client.cookies["ft"] == f'"{token_str}"'


@pytest.mark.asyncio
async def test_handshake_cookie_application_order() -> None:
    token_str = "4b42377b-e10c-482d-8b06-4df0da00ad20:dGhpcy1pcy1hLXZhbGlkLXFyYXRvci1mdC10b2tlbg=="

    class OrderTrackingClient:
        def __init__(self) -> None:
            self.cookies: dict[str, str] = {}
            self.post_cookies: dict[str, str] = {}
            self.pixel_cookies: dict[str, str] = {}

        async def post(self, url: str, **kwargs: Any) -> FakeResponse:
            self.post_cookies = dict(self.cookies)
            return FakeResponse(200, json.dumps(token_str))

        async def get(self, url: str, **kwargs: Any) -> FakeResponse:
            self.pixel_cookies = dict(self.cookies)
            return FakeResponse(200, "")

    client = OrderTrackingClient()
    res = await perform_qrator_handshake(client, slot_id="slot-1", identity="user_a")
    assert res.is_success is True

    # During requests cookies are staged; client.cookies is untouched until final commit
    assert not client.post_cookies and not client.pixel_cookies
    assert client.cookies.get("f", "").startswith("5.")
    assert client.cookies.get("ft") == f'"{token_str}"'


def test_extract_ft_token_rejects_error_json() -> None:
    error_payload = json.dumps({"error": "something went wrong on server" * 3})
    assert _extract_ft_token(error_payload) is None


@pytest.mark.asyncio
async def test_handshake_propagates_http_deferred() -> None:
    class DeferredClient:
        def __init__(self) -> None:
            self.cookies: dict[str, str] = {}

        async def post(self, url: str, **kwargs: Any) -> FakeResponse:
            raise HttpDeferred("cooldown", 999999999.0)

    with pytest.raises(HttpDeferred):
        await perform_qrator_handshake(DeferredClient(), slot_id="slot-1", identity="user_a")


@pytest.mark.asyncio
async def test_handshake_fails_on_pixel_429() -> None:
    token_str = "4b42377b-e10c-482d-8b06-4df0da00ad20:dGhpcy1pcy1hLXZhbGlkLXFyYXRvci1mdC10b2tlbg=="

    class RateLimitedPixelClient:
        def __init__(self) -> None:
            self.cookies: dict[str, str] = {}

        async def post(self, url: str, **kwargs: Any) -> FakeResponse:
            return FakeResponse(200, json.dumps(token_str))

        async def get(self, url: str, **kwargs: Any) -> FakeResponse:
            return FakeResponse(429, "rate limited", headers={"Retry-After": "900"})

    res = await perform_qrator_handshake(RateLimitedPixelClient(), slot_id="slot-1", identity="user_a")
    assert res.is_success is False
    assert res.outcome == HandshakeOutcome.RATE_LIMITED
    assert res.retry_after_sec == 900.0

