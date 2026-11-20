from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
import pytest
import httpx

from hunter.application.slot_supervisor import _compute_backoff
from hunter.domain.slots import SlotSignal
from hunter.transport.challenge import should_attempt_challenge
from hunter.transport.geetest_solver import _get_firewall_captcha_id
from hunter.transport.http_fetch import _default_browser_headers


def test_compute_backoff_uses_62s_for_ip_and_challenge_tier():
    assert _compute_backoff(SlotSignal.CHALLENGE_DETECTED, 0, None) == 62.0
    assert _compute_backoff(SlotSignal.HTTP_429, 0, None) == 62.0
    assert _compute_backoff(SlotSignal.HTTP_403, 0, None) == 62.0
    assert _compute_backoff(SlotSignal.CHALLENGE_DETECTED, 1, None) == 120.0


def test_should_attempt_challenge_on_firewall_dispatcher():
    class Fake403:
        status_code = 403
        text = '{"too-many-requests": {"link": "ru.avito:\\/\\/1\\/firewall\\/captcha\\/show"}}'
        headers = {}
        cookies = {}

    assert should_attempt_challenge(Fake403()) is True


def test_default_browser_headers_include_referer_and_same_origin():
    hdrs = _default_browser_headers("Mozilla/5.0 (X11; Linux x86_64; rv:135.0) Firefox/135.0")
    assert hdrs.get("Referer") == "https://www.avito.ru/"
    assert hdrs.get("sec-fetch-site") == "same-origin"


@pytest.mark.asyncio
async def test_get_firewall_captcha_id_extracts_or_dispatches():
    # 1. From HTML
    c = AsyncMock()
    cid = await _get_firewall_captcha_id(c, 'var captchaId = "11223344556677889900aabbccddeeff";')
    assert cid == "11223344556677889900aabbccddeeff"

    # 2. Dispatcher fallback
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json = MagicMock(return_value={"success": {"result": {"captcha": {"geeTest": {"captcha_id": "aabbccddeeff11223344556677889900"}}}}})
    c.post = AsyncMock(return_value=fake_resp)
    cid2 = await _get_firewall_captcha_id(c, "")
    assert cid2 == "aabbccddeeff11223344556677889900"
