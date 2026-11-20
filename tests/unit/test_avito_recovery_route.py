from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import httpx
import pytest

from hunter.collect.firewall_pow import solve_firewall_pow_http
from hunter.transport.catalog_api import warmup_client_session
from hunter.transport.geetest_solver import solve_geetest_v4_flow


class FakeJar(dict):
    def set(self, name: str, value: str, domain: str = "", path: str = "") -> None:
        self[name] = value


@pytest.mark.asyncio
async def test_warmup_forwards_origin_client_and_endpoint() -> None:
    client = SimpleNamespace(
        headers={"User-Agent": "audit-UA"},
        cookies=FakeJar(),
        get=AsyncMock(side_effect=[
            SimpleNamespace(status_code=439, text='{"pow_challenge":"audit-token"}', cookies={}),
            SimpleNamespace(status_code=200, text='<html>catalog</html>', cookies={}),
        ]),
    )
    solver = AsyncMock(return_value=(True, [{"name": "u", "value": "val", "domain": ".avito.ru"}]))
    with patch("hunter.collect.firewall_pow.solve_firewall_pow_http", solver), patch("hunter.transport.session_warmup.asyncio.sleep", new=AsyncMock()):
        await warmup_client_session(client, "https://www.avito.ru/moskva", endpoint="http://proxy:8080")

    assert solver.await_count == 1
    kwargs = solver.await_args.kwargs
    assert kwargs.get("http_client") is client, "warmup must forward original client"
    assert kwargs.get("endpoint") == "http://proxy:8080", "warmup must forward original endpoint"


@pytest.mark.asyncio
async def test_geetest_flow_uses_endpoint_proxy_and_forbids_direct_fallback() -> None:
    endpoint_proxy = "http://audit-proxy:8080"
    created_clients = []

    def mock_async_client(**kwargs):
        c = AsyncMock()
        c.kwargs = kwargs
        c.cookies = FakeJar()
        c.get = AsyncMock(side_effect=httpx.ConnectError("proxy unreachable"))
        c.post = AsyncMock(return_value=SimpleNamespace(status_code=200, json=lambda: {}))
        c.aclose = AsyncMock()
        created_clients.append(c)
        return c

    with patch("httpx.AsyncClient", side_effect=mock_async_client):
        solved, _ = await solve_geetest_v4_flow(endpoint=endpoint_proxy, html="<html></html>")
        assert not solved
        assert len(created_clients) >= 1
        assert created_clients[0].kwargs.get("proxy") == endpoint_proxy


@pytest.mark.asyncio
async def test_borrowed_client_is_not_closed() -> None:
    borrowed_client = AsyncMock()
    borrowed_client.aclose = AsyncMock()
    borrowed_client.cookies = FakeJar()
    borrowed_client.get = AsyncMock(return_value=SimpleNamespace(status_code=200, text="{}", cookies={}))
    borrowed_client.post = AsyncMock(return_value=SimpleNamespace(status_code=200, json=lambda: {}))

    await solve_geetest_v4_flow(client=borrowed_client, html="<html></html>")
    assert borrowed_client.aclose.await_count == 0, "borrowed client must not be closed"


@pytest.mark.asyncio
async def test_pow_borrowed_client_is_not_closed() -> None:
    borrowed_client = AsyncMock()
    borrowed_client.aclose = AsyncMock()
    borrowed_client.cookies = FakeJar()
    borrowed_client.post = AsyncMock(return_value=SimpleNamespace(status_code=200, json=lambda: {"verified": True}, cookies={}))

    # solve pow with dummy jwt token: e30.e30.e30 (three base64url empty json dicts)
    dummy_jwt = "eyJhbGciOiJub25lIn0.eyJleHAiOjk5OTk5OTk5OTksImJpdHMiOjB9."
    await solve_firewall_pow_http(dummy_jwt, http_client=borrowed_client)
    assert borrowed_client.aclose.await_count == 0, "borrowed client must not be closed"


@pytest.mark.asyncio
async def test_runtime_handle_challenge_passes_borrowed_client_ua_and_credentials() -> None:
    from hunter.application.public_poll_runtime import PublicPollRuntime, QueryAssignment
    from hunter.domain.poll_policy import PollPolicy
    from hunter.transport.http_fetch import SlotFetchResponse
    from hunter.transport.proxy_credentials import ProxyCredentials

    borrowed_client = AsyncMock()
    borrowed_client.headers = {"User-Agent": "audit-custom-UA"}
    borrowed_client.aclose = AsyncMock()
    fetcher = SimpleNamespace(
        curl_client_b=borrowed_client,
        proxy_credentials=ProxyCredentials("proxy_user", "proxy_pass"),
        load_cookies_from_store=AsyncMock(return_value=0),
    )
    pool = SimpleNamespace(get_fetcher=lambda *a: fetcher, _fetchers={"audit": fetcher})
    service = SimpleNamespace(handle_challenge=AsyncMock(return_value=SimpleNamespace(solved=True, status="solved")))
    rt = PublicPollRuntime(None, pool, AsyncMock(), PollPolicy(), challenge_service=service)

    asg = QueryAssignment("audit", "http://proxy.test:8080", "q1", "https://www.avito.ru/q1", 1, 0)
    resp = SlotFetchResponse(
        status_code=439, url="https://www.avito.ru/q1",
        text='{"pow_challenge":"token"}',
        client_identity="user_b",
        headers={"Server": "nginx"},
    )
    await rt._handle_challenge(asg, resp)

    service.handle_challenge.assert_awaited_once()
    req = service.handle_challenge.await_args.args[0]
    assert req.http_client is borrowed_client, "Must forward borrowed client"
    assert req.headers.get("User-Agent") == "audit-custom-UA", "Must forward request User-Agent from client"
    assert req.proxy_credentials.username == "proxy_user", "Must forward proxy credentials"
    assert req.endpoint == "http://proxy.test:8080", "Must forward endpoint"
    assert borrowed_client.aclose.await_count == 0, "Borrowed client must not be closed"

