"""Unit tests verifying bot leak fixes (pow_* expiry/cleanup, _adcc expiry, UA/TLS alignment)."""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from hunter.transport.browser_headers import _default_browser_headers
from hunter.transport.http_fetch import (
    DEFAULT_CHROME_USER_AGENT,
    DEFAULT_FIREFOX_USER_AGENT,
    platform_firefox_user_agent,
)
from hunter.transport.recovery_transport import (
    apply_scoped_cookies_to_client,
    merge_scoped_cookies,
    update_cookie_list_from_jar,
)


class MockCookieJarClient:
    def __init__(self) -> None:
        self.cookies = MagicMock()
        self.set_cookies: dict[str, dict[str, str]] = {}

        def _set(name: str, val: str, domain: str = ".avito.ru", path: str = "/") -> None:
            self.set_cookies[name] = {"value": val, "domain": domain, "path": path}

        self.cookies.set.side_effect = _set


def test_ua_tls_version_harmonization() -> None:
    # 1. Firefox version matches impersonate="firefox133"
    assert "rv:133.0" in DEFAULT_FIREFOX_USER_AGENT
    assert "Firefox/133.0" in DEFAULT_FIREFOX_USER_AGENT
    assert "Firefox/135.0" not in DEFAULT_FIREFOX_USER_AGENT

    # 2. Chrome version matches impersonate="chrome131"
    assert "Chrome/131.0" in DEFAULT_CHROME_USER_AGENT
    assert "Chrome/133.0" not in DEFAULT_CHROME_USER_AGENT

    # 3. Sec-CH-UA matches Chrome 131
    chrome_headers = _default_browser_headers(DEFAULT_CHROME_USER_AGENT)
    sec_ch_ua = chrome_headers.get("sec-ch-ua", "")
    assert '"Google Chrome";v="131"' in sec_ch_ua
    assert '"Chromium";v="131"' in sec_ch_ua
    assert 'v="133"' not in sec_ch_ua


def test_pow_solved_expires_after_ttl() -> None:
    now = time.time()
    cookies = [
        {"name": "_adcc", "value": "clearance123", "domain": ".avito.ru", "path": "/", "expires": now + 86400},
        {"name": "pow_solved", "value": "1", "domain": ".avito.ru", "path": "/", "expires": now + 10},
    ]

    client = MockCookieJarClient()
    # At now: pow_solved is valid
    apply_scoped_cookies_to_client(client, cookies)
    assert "pow_solved" in client.set_cookies

    # After 15 seconds: pow_solved is expired and must NOT be applied to client
    client_after = MockCookieJarClient()
    cookies_expired = [
        {"name": "_adcc", "value": "clearance123", "domain": ".avito.ru", "path": "/", "expires": now + 86400},
        {"name": "pow_solved", "value": "1", "domain": ".avito.ru", "path": "/", "expires": now - 5},
    ]
    apply_scoped_cookies_to_client(client_after, cookies_expired)
    assert "pow_solved" not in client_after.set_cookies
    assert "_adcc" in client_after.set_cookies


def test_legacy_pow_solved_without_expires_pruned_when_adcc_present() -> None:
    # Legacy unexpired pow_solved in stored session file
    now = time.time()
    cookies = [
        {"name": "_adcc", "value": "clearance123", "domain": ".avito.ru", "path": "/", "expires": now + 86400},
        {"name": "pow_solved", "value": "1", "domain": ".avito.ru", "path": "/"},  # No expires!
    ]
    client = MockCookieJarClient()
    apply_scoped_cookies_to_client(client, cookies)
    # When clearance _adcc is established, obsolete pow_solved without TTL must not be sent
    assert "pow_solved" not in client.set_cookies
    assert "_adcc" in client.set_cookies


def test_pow_challenge_pruned_after_adcc_acquired() -> None:
    now = time.time()
    existing = [
        {"name": "u", "value": "user123", "domain": ".avito.ru", "path": "/"},
        {"name": "pow_challenge", "value": "old_jwt_token", "domain": ".avito.ru", "path": "/"},
    ]
    incoming = [
        {"name": "_adcc", "value": "clearance123", "domain": ".avito.ru", "path": "/", "expires": now + 86400},
    ]

    merged = merge_scoped_cookies(existing, incoming)
    merged_names = {c["name"] for c in merged}

    assert "_adcc" in merged_names
    assert "u" in merged_names
    assert "pow_challenge" not in merged_names  # Spent challenge token is purged!


def test_adcc_and_pow_solved_expiration_in_update_cookie_list_from_jar() -> None:
    cookies = [{"name": "u", "value": "user123", "domain": ".avito.ru", "path": "/"}]
    jar = {
        "_adcc": "new_clearance_token",
        "pow_solved": "1",
        "pow_challenge": "consumed_jwt",
    }

    now = time.time()
    res = update_cookie_list_from_jar(cookies, jar)
    res_by_name = {c["name"]: c for c in res}

    # 1. _adcc must have valid ~24h expiration
    assert "_adcc" in res_by_name
    adcc_exp = res_by_name["_adcc"].get("expires")
    assert adcc_exp is not None
    assert (now + 86300) <= float(adcc_exp) <= (now + 86500)

    # 2. pow_solved must have 10s expiration
    assert "pow_solved" in res_by_name
    ps_exp = res_by_name["pow_solved"].get("expires")
    assert ps_exp is not None
    assert (now + 8) <= float(ps_exp) <= (now + 12)

    # 3. pow_challenge must be purged since _adcc is present
    assert "pow_challenge" not in res_by_name


def test_merge_scoped_cookies_assigns_default_adcc_expiry_when_none() -> None:
    # Existing session cookie on disk with expires=None (historical session artifact)
    existing = [
        {"name": "u", "value": "user123", "domain": ".avito.ru", "path": "/"},
        {"name": "_adcc", "value": "clearance123", "domain": ".avito.ru", "path": "/"},  # expires is None!
    ]
    incoming = [
        {"name": "f", "value": "fingerprint123", "domain": ".avito.ru", "path": "/"},
        {"name": "ft", "value": "ft_token123", "domain": ".avito.ru", "path": "/"},
    ]
    now = time.time()
    merged = merge_scoped_cookies(existing, incoming)
    merged_by_name = {c["name"]: c for c in merged}

    assert "_adcc" in merged_by_name
    adcc_exp = merged_by_name["_adcc"].get("expires")
    assert adcc_exp is not None
    assert (now + 86300) <= float(adcc_exp) <= (now + 86500)


@pytest.mark.asyncio
async def test_x_cube_timing_simulation() -> None:
    from hunter.transport.geetest_solver import generate_x_cube_timing, submit_avito_captcha_verify

    # 1. generate_x_cube_timing format and range
    for _ in range(50):
        timing_str = generate_x_cube_timing()
        assert "." in timing_str
        val = float(timing_str)
        # Realistic desktop 50-step gl.readPixels benchmark latency: 130ms - 260ms
        assert 130.0 <= val <= 260.0
        # Must not be an integer string like '42'
        assert not timing_str.isdigit()

    # 2. submit_avito_captcha_verify sends realistic X-Cube header
    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"success": {"result": {"verified": True}}}

    async def _mock_post(url: str, **kwargs: Any) -> Any:
        mock_client.last_headers = kwargs.get("headers", {})
        return mock_resp

    mock_client.post.side_effect = _mock_post

    verified = await submit_avito_captcha_verify(
        mock_client,
        seccode={"lot_number": "1", "pass_token": "2", "gen_time": "3", "captcha_output": "4"},
        referer="https://www.avito.ru/",
    )
    assert verified is True
    x_cube = mock_client.last_headers.get("X-Cube", "")
    assert "." in x_cube
    assert 130.0 <= float(x_cube) <= 260.0
    assert not (10 <= float(x_cube) <= 99 and x_cube.isdigit())


