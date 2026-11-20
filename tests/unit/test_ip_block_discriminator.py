"""Unit tests for IP block discriminator preventing peer retry hammering on banned IP."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import pytest

from hunter.transport.classification import is_ip_level_block
from hunter.application.public_poll_runtime import PublicPollRuntime, QueryAssignment
from hunter.domain.poll_policy import PollPolicy
from hunter.domain.slots import SlotSignal
from hunter.transport.http_fetch import HttpSlotFetcher, SlotFetchConfig


class MockResponse:
    def __init__(self, status: int, text: str = "", headers: dict | None = None):
        self.status_code, self.text, self.body = status, text, text
        self.headers, self.cookies = headers or {}, {}
        self.url, self.error = "https://www.avito.ru/moskva", None

    async def aiter_content(self):
        yield self.body.encode()

    async def aclose(self):
        pass


def test_is_ip_level_block_detection() -> None:
    """Verifies that is_ip_level_block correctly identifies hard IP-level blocks vs session blocks."""
    # 1. 403 with "IP-адрес временно заблокирован" (hard IP ban)
    r1 = MockResponse(403, "Ваш IP-адрес временно заблокирован системой защиты")
    assert is_ip_level_block(r1) is True

    # 2. 403 with Cloudflare / WAF block
    r2 = MockResponse(403, "Blocked by Cloudflare - error code 1020")
    assert is_ip_level_block(r2) is True

    # 3. 403 with security-firewall-block
    r3 = MockResponse(403, "<div class='security-firewall-block'>Blocked</div>")
    assert is_ip_level_block(r3) is True

    # 4. Real Avito hard block: 403 len=7942, title says "проблема с IP"
    r4 = MockResponse(403, "<html><title>Доступ ограничен: проблема с IP</title></html>")
    assert is_ip_level_block(r4) is True

    # 4b. Body may contain NBSP between "с" and "IP"
    r4b = MockResponse(403, "<h1>Страница не загрузилась из-за проблем с\u00a0IP</h1>")
    assert is_ip_level_block(r4b) is True

    # 5. Captcha/PoW page: similar "Доступ ограничен" wording is NOT an IP ban
    r5 = MockResponse(
        403,
        "<html><head><title>Доступ ограничен: проверка безопасности</title></head>"
        "<body><div class='firewall__title'>Нужна небольшая проверка браузера</div>"
        "<p>IP-адрес — узнать его можно</p>"
        "<p>Что делать, если проблема повторяется</p>"
        "<script>pow_challenge</script></body></html>",
    )
    assert is_ip_level_block(r5) is False

    # 6. Generic "Доступ запрещен" is a WAF/session block, not necessarily IP-level
    r6 = MockResponse(403, "<html><body>Доступ запрещен</body></html>")
    assert is_ip_level_block(r6) is False

    # 7. 200 OK response (never an IP block)
    r7 = MockResponse(200, "<html>OK</html>")
    assert is_ip_level_block(r7) is False

    # 8. None response
    assert is_ip_level_block(None) is False


@pytest.mark.asyncio
async def test_peer_failover_skipped_on_ip_level_block() -> None:
    """When 403 is a hard IP-level block, peer failover on the same IP must be skipped entirely."""
    fetcher = HttpSlotFetcher("test-slot", endpoint="direct", config=SlotFetchConfig(interleave_sessions=True))
    fetcher._session_index = 0
    fetcher._user_b_ready = True

    asg = QueryAssignment("test-slot", "direct", "q-1", "https://www.avito.ru/moskva/tovary", 1, 0)
    sigs = []
    rt = PublicPollRuntime(
        None, SimpleNamespace(get_fetcher=lambda *a: fetcher, _fetchers={"test-slot": fetcher}),
        AsyncMock(return_value={"items_count": 1}), PollPolicy(),
        slot_signal_handler=AsyncMock(side_effect=lambda s, sig, **kw: sigs.append(sig)),
        challenge_service=SimpleNamespace(handle_challenge=AsyncMock(return_value=SimpleNamespace(solved=False))),
    )

    ip_ban_html = "<html><body>Ваш IP-адрес временно заблокирован системой защиты</body></html>"
    c_ip_ban = MockResponse(403, ip_ban_html)

    try:
        with patch("hunter.application.public_poll_runtime.resolve_poll_target", return_value=("https://www.avito.ru/web/1/js/items", {})), \
             patch("hunter.application.public_poll_runtime.record_poll_response"), \
             patch("hunter.application.public_poll_runtime.asyncio.sleep", new=AsyncMock()) as mock_sleep:

            with patch.object(fetcher.curl_client, "get", new=AsyncMock(return_value=c_ip_ban)) as ga, \
                 patch.object(fetcher.curl_client_b, "get", new=AsyncMock(return_value=MockResponse(200, "OK"))) as gb:

                res = await rt.poll(asg, sequence=1)
                assert res is None
                assert ga.await_count == 1
                # CRITICAL: Peer client on same IP must NOT be hammered!
                assert gb.await_count == 0
                # Slot must be quarantined immediately
                assert SlotSignal.HTTP_403 in sigs or SlotSignal.CHALLENGE_DETECTED in sigs
                # Must not sleep 4s for peer failover
                assert not any(call.args and call.args[0] == 4.0 for call in mock_sleep.call_args_list)
    finally:
        await fetcher.close()


@pytest.mark.asyncio
async def test_peer_failover_retains_recovery_for_session_403() -> None:
    """When 403 is NOT an IP block, peer failover must still execute and recover the request."""
    fetcher = HttpSlotFetcher("test-slot", endpoint="direct", config=SlotFetchConfig(interleave_sessions=True))
    fetcher._session_index = 0
    fetcher._user_b_ready = True

    asg = QueryAssignment("test-slot", "direct", "q-1", "https://www.avito.ru/moskva/tovary", 1, 0)
    rt = PublicPollRuntime(
        None, SimpleNamespace(get_fetcher=lambda *a: fetcher, _fetchers={"test-slot": fetcher}),
        AsyncMock(return_value={"items_count": 1}), PollPolicy(),
        slot_signal_handler=AsyncMock(),
        challenge_service=SimpleNamespace(handle_challenge=AsyncMock(return_value=SimpleNamespace(solved=False))),
    )

    feed_html = (
        '<!DOCTYPE html><html><body><script data-mfe-state="true">'
        + '{"loaderData": {"data": {"catalog": {"items": [{"id": "1", "priceDetailed": {"value": 100}}]}}}}'
        + '</script></body></html>'
    )
    c_session_403 = MockResponse(403, "Tokenless session barrier")
    c_ok = MockResponse(200, feed_html)

    try:
        with patch("hunter.application.public_poll_runtime.resolve_poll_target", return_value=("https://www.avito.ru/web/1/js/items", {})), \
             patch("hunter.application.public_poll_runtime.record_poll_response"), \
             patch("hunter.application.public_poll_runtime.asyncio.sleep", new=AsyncMock()):

            with patch.object(fetcher.curl_client, "get", new=AsyncMock(return_value=c_session_403)) as ga, \
                 patch.object(fetcher.curl_client_b, "get", new=AsyncMock(return_value=c_ok)) as gb:

                res = await rt.poll(asg, sequence=1)
                assert res is not None
                assert ga.await_count == 1
                assert gb.await_count == 1  # Peer failover was executed and rescued the poll!
    finally:
        await fetcher.close()
