import httpx
import pytest
from hunter.transport.challenge_dispatch import (
    CaptchaProviderType,
    parse_captcha_dispatch_payload,
)
from hunter.transport.geetest_solver import solve_geetest_v4_flow

def test_parse_captcha_dispatch_types():
    assert parse_captcha_dispatch_payload({"hCaptcha": {"type": "hCaptcha"}}).provider_type == CaptchaProviderType.HCAPTCHA
    assert parse_captcha_dispatch_payload({"internal": True}).provider_type == CaptchaProviderType.INTERNAL
    assert parse_captcha_dispatch_payload({"disabled": True}).provider_type == CaptchaProviderType.DISABLED
    assert parse_captcha_dispatch_payload({"other": 1}).provider_type == CaptchaProviderType.UNKNOWN
    gt = parse_captcha_dispatch_payload({"geeTest": {"captchaId": "a" * 32}})
    assert gt.provider_type == CaptchaProviderType.GEETEST and gt.is_supported and gt.captcha_id == "a" * 32

@pytest.mark.asyncio
async def test_hcaptcha_does_not_trigger_geetest_load():
    reqs = []
    def handler(req: httpx.Request) -> httpx.Response:
        reqs.append(str(req.url))
        if "/firewallCaptcha/get" in str(req.url):
            return httpx.Response(200, json={"success": {"result": {"captcha": {"hCaptcha": {"type": "hCaptcha"}}}}})
        return httpx.Response(200, json={"data": {}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        solved, cookies = await solve_geetest_v4_flow(client=client)
        assert not solved and cookies == [] and not any("geevisit.com" in u for u in reqs)

@pytest.mark.asyncio
async def test_unknown_and_disabled_captcha_does_not_trigger_geetest_load():
    reqs = []
    def handler(req: httpx.Request) -> httpx.Response:
        reqs.append(str(req.url))
        if "/firewallCaptcha/get" in str(req.url):
            return httpx.Response(200, json={"success": {"result": {"captcha": {"unknownCaptcha": 1}}}})
        return httpx.Response(200, json={"data": {}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        solved, cookies = await solve_geetest_v4_flow(client=client)
        assert not solved and cookies == [] and not any("geevisit.com" in u for u in reqs)

@pytest.mark.asyncio
async def test_supported_geetest_triggers_load_with_actual_id():
    reqs = []
    def handler(req: httpx.Request) -> httpx.Response:
        reqs.append(str(req.url))
        if "/firewallCaptcha/get" in str(req.url):
            return httpx.Response(200, json={"success": {"result": {"captcha": {"geeTest": {"captchaId": "c" * 32}}}}})
        return httpx.Response(200, json={"data": {}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await solve_geetest_v4_flow(client=client)
        assert any(f"captcha_id={'c'*32}" in u for u in reqs)


@pytest.mark.asyncio
async def test_geetest_v4_fast_path_skips_firewall_get():
    reqs = []
    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        reqs.append(url)
        if "/firewallCaptcha/get" in url:
            return httpx.Response(200, json={"success": {"result": {"captcha": {"hCaptcha": {}}}}})
        if "/load" in url:
            return httpx.Response(200, text='geetest_123({"data": {"lot_number": "1234567890123456789012345678", "captcha_type": "click"}})')
        if "/verify" in url and "geevisit.com" in url:
            return httpx.Response(200, text='geetest_123({"data": {"seccode": {"result": "ok"}}})')
        if "/firewallCaptcha/verify" in url:
            return httpx.Response(200, json={"success": {"result": {"verified": True}}})
        return httpx.Response(200, json={"data": {}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        solved, _ = await solve_geetest_v4_flow(client=client, fast_path=True)
        assert solved is True
        assert not any("/firewallCaptcha/get" in u for u in reqs)


@pytest.mark.asyncio
async def test_geetest_v4_fast_path_fallback_to_get_on_failure():
    reqs = []
    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        reqs.append(url)
        if "/load" in url:
            # GeeTest rejects fast-path
            return httpx.Response(200, text='geetest_123({"data": {}})')
        if "/firewallCaptcha/get" in url:
            # Fallback queries /get and gets hCaptcha
            return httpx.Response(200, json={"success": {"result": {"captcha": {"hCaptcha": {}}}}})
        return httpx.Response(200, json={"data": {}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        solved, cookies = await solve_geetest_v4_flow(client=client, fast_path=True)
        assert solved is False
        assert cookies == []
        assert any("/firewallCaptcha/get" in u for u in reqs)

