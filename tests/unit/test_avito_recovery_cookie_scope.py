"""Unit tests for recovery cookie domain scope, provider isolation, and expiry preservation (T-978)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from hunter.collect.firewall_pow import solve_firewall_pow_http
from hunter.transport.challenge import _merge_cookies
from hunter.transport.geetest_solver import solve_geetest_v4_flow


@pytest.mark.asyncio
async def test_avito_cookies_not_leaked_to_provider_hosts() -> None:
    """Avito cookies must not become hostless and must not be sent to provider hosts like geevisit."""
    requests: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        requests.append({"host": req.url.host, "cookie": req.headers.get("cookie", "")})
        if req.url.path.endswith("/get"):
            return httpx.Response(200, json={"success": {"result": {"captcha": {"geeTest": {"captchaId": "a" * 32}}}}})
        if req.url.host == "gcaptcha4.geevisit.com" and req.url.path == "/load":
            return httpx.Response(200, json={"data": {"lot_number": "audit-lot", "captcha_type": "audit"}})
        return httpx.Response(200, json={"data": {}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with (
            patch("hunter.transport.geetest_solver._solve_geetest_w", new=AsyncMock(return_value="w_val")),
            patch("hunter.transport.geetest_solver.submit_avito_captcha_verify", new=AsyncMock(return_value=True)),
        ):
            await solve_geetest_v4_flow(client=client, cookies={"u": "audit-secret-avito-cookie", "sess": "avito-session"})

    provider_reqs = [r for r in requests if r["host"] == "gcaptcha4.geevisit.com"]
    assert provider_reqs, "Expected requests to geevisit.com"
    for r in provider_reqs:
        assert "audit-secret-avito-cookie" not in r["cookie"]


@pytest.mark.asyncio
async def test_provider_cookie_not_exported_as_avito() -> None:
    """Cookies set by provider domains must not be remapped to .avito.ru."""
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/get"):
            return httpx.Response(200, json={"success": {"result": {"captcha": {"geeTest": {"captchaId": "b" * 32}}}}})
        if req.url.host == "gcaptcha4.geevisit.com" and req.url.path == "/load":
            return httpx.Response(
                200, json={"data": {"lot_number": "audit-lot", "captcha_type": "audit"}},
                headers={"set-cookie": "provider_cookie=audit_val; Path=/; Domain=geevisit.com"},
            )
        if req.url.host == "gcaptcha4.geevisit.com" and req.url.path == "/verify":
            return httpx.Response(200, json={"data": {"seccode": {"result": "ok"}}})
        return httpx.Response(200, json={"success": {"result": {"verified": True}}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with (
            patch("hunter.transport.geetest_solver._solve_geetest_w", new=AsyncMock(return_value="w_val")),
            patch("hunter.transport.geetest_solver.submit_avito_captcha_verify", new=AsyncMock(return_value=True)),
        ):
            verified, cookies = await solve_geetest_v4_flow(client=client)

    assert verified is True
    remapped = [c for c in cookies if c.get("name") == "provider_cookie" and c.get("domain") in (".avito.ru", "avito.ru")]
    assert not remapped, f"Provider cookie was erroneously remapped to .avito.ru: {remapped}"


def test_merge_cookies_preserves_scope_and_expiry() -> None:
    """Cookies with same name on different domains/paths or with expiry must be preserved."""
    existing = [
        {"name": "sid", "value": "val_root", "domain": ".avito.ru", "path": "/", "expires": 1700000000},
        {"name": "sid", "value": "val_mobile", "domain": "m.avito.ru", "path": "/", "expires": 1800000000},
    ]
    incoming = [
        {"name": "sid", "value": "val_subpath", "domain": ".avito.ru", "path": "/api", "expires": 1900000000},
        {"name": "fpc", "value": "fpc_val", "domain": ".avito.ru", "path": "/", "expires": 2000000000},
    ]
    merged = _merge_cookies(existing, incoming)
    assert len([c for c in merged if c["name"] == "sid"]) == 3
    assert next(c for c in merged if c["name"] == "fpc").get("expires") == 2000000000


@pytest.mark.asyncio
async def test_firewall_pow_http_preserves_domain_and_expiry() -> None:
    """solve_firewall_pow_http preserves domain, path, and expiry on existing cookies."""
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/get"):
            return httpx.Response(200, json={"success": {"result": {"challenge_jwt": "e30.eyJpZCI6ICJhIiwgImNvbXBsIjogMX0=.sig"}}})
        if req.url.path.endswith("/verify"):
            return httpx.Response(200, json={"success": {"result": {"verified": True}}})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        input_cookies = [{"name": "existing_token", "value": "tok123", "domain": ".avito.ru", "path": "/special", "expires": 1799999999}]
        with patch("hunter.collect.firewall_pow.find_nonce", return_value=42):
            solved, out_cookies = await solve_firewall_pow_http("test-token", cookies=input_cookies, http_client=client)

    assert solved is True
    existing = next((c for c in out_cookies if c["name"] == "existing_token"), None)
    assert existing is not None and existing["path"] == "/special" and existing["expires"] == 1799999999


def test_expired_cookie_not_attached_to_outgoing_requests() -> None:
    """Expired cookies (expires <= now) must not be applied to HTTP client."""
    from hunter.transport.recovery_transport import apply_scoped_cookies_to_client
    with httpx.Client() as client:
        apply_scoped_cookies_to_client(client, [{"name": "expired_c", "value": "old", "domain": ".avito.ru", "path": "/", "expires": 1}])
        req = client.build_request("GET", "https://www.avito.ru/")
        assert "expired_c" not in req.headers.get("cookie", "")


def test_update_cookie_list_preserves_distinct_paths_for_same_name() -> None:
    """Cookies with same name on / and /special must retain distinct path values across roundtrip."""
    from hunter.transport.recovery_transport import update_cookie_list_from_jar
    original = [
        {"name": "u", "value": "root", "domain": ".avito.ru", "path": "/"},
        {"name": "u", "value": "special", "domain": ".avito.ru", "path": "/special"},
    ]
    merged = update_cookie_list_from_jar(original, {"u": "special"})
    values = [c["value"] for c in merged]
    assert values == ["root", "special"]


@pytest.mark.asyncio
async def test_future_cookie_expires_when_client_time_advances() -> None:
    """Cookies with future expiry must have expires set in jar and expire when clock passes deadline."""
    import time
    from hunter.transport.recovery_transport import apply_scoped_cookies_to_client

    now = time.time()
    async with httpx.AsyncClient() as client:
        apply_scoped_cookies_to_client(
            client,
            [{"name": "future_c", "value": "val", "domain": ".avito.ru", "path": "/", "expires": now + 60}],
        )
        entry = next(c for c in client.cookies.jar if c.name == "future_c")
        assert entry.expires is not None
        assert entry.expires == int(now + 60)
        with patch("http.cookiejar.time.time", return_value=now + 30):
            req = client.build_request("GET", "https://www.avito.ru/")
            assert "future_c=val" in req.headers.get("cookie", "")
        with patch("http.cookiejar.time.time", return_value=now + 65):
            req = client.build_request("GET", "https://www.avito.ru/")
            assert "future_c" not in req.headers.get("cookie", "")

