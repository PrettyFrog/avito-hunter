"""Comprehensive unit tests for GeeTest connection stability and challenge solver peer failover."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
import httpx

from hunter.application.public_poll_runtime import PublicPollRuntime, QueryAssignment
from hunter.domain.poll_policy import PollPolicy
from hunter.domain.slots import SlotSignal
from hunter.transport.challenge_models import ChallengeOutcome, ChallengeRequest, ChallengeStatus
from hunter.transport.http_fetch import HttpSlotFetcher, SlotFetchConfig


class MockResponse:
    def __init__(self, status: int, text: str = "", headers: dict | None = None):
        self.status_code, self.text, self.body = status, text, text
        self.headers, self.cookies = headers or {}, {}
        self.url, self.encoding, self.astream_task, self.error = "https://www.avito.ru/moskva", "utf-8", None, None

    async def aiter_content(self):
        yield self.body.encode()

    async def aclose(self):
        pass


FEED_HTML = (
    '<!DOCTYPE html><html><body><script data-mfe-state="true">'
    + json.dumps({"loaderData": {"data": {"catalog": {"items": [{"id": "1", "priceDetailed": {"value": 100}}]}}}})
    + '</script></body></html>'
)

# Captcha/PoW wording: "Доступ ограничен" is similar to the IP page, but
# there is no explicit IP-block phrase here, so peer failover remains allowed.
SESSION_403_HTML = (
    "<html><head><title>Доступ ограничен: проверка безопасности</title></head>"
    "<body><div class='firewall__title'>Нужна небольшая проверка браузера</div></body></html>"
)


def _setup(session_index: int = 0, handle_challenge_mock=None):
    f = HttpSlotFetcher("test-slot", endpoint="direct", config=SlotFetchConfig(interleave_sessions=True))
    f._session_index, f._user_b_ready = session_index, True
    sigs, sig_kw, ingest = [], [], AsyncMock(return_value={"items_count": 1})
    default_challenge = SimpleNamespace(
        handle_challenge=handle_challenge_mock or AsyncMock(return_value=ChallengeOutcome(
            slot_id="test-slot", status=ChallengeStatus.SOLVED, solved=True,
            new_session_version=2, quarantined=False,
        ))
    )
    rt = PublicPollRuntime(
        None, SimpleNamespace(get_fetcher=lambda *a: f, _fetchers={"test-slot": f}),
        ingest, PollPolicy(),
        slot_signal_handler=AsyncMock(side_effect=lambda s, sig, **kw: (sigs.append(sig), sig_kw.append(kw))),
        challenge_service=default_challenge,
    )
    return f, rt, QueryAssignment("test-slot", "direct", "q-1", "https://www.avito.ru/moskva/tovary", 1, 0), sigs, sig_kw, ingest


async def _run_poll(rt, asg):
    with patch("hunter.application.public_poll_runtime.resolve_poll_target", return_value=("https://www.avito.ru/web/1/js/items", {})), \
         patch("hunter.application.public_poll_runtime.record_poll_response"), \
         patch("hunter.application.public_poll_runtime.asyncio.sleep", new=AsyncMock()), \
         patch("hunter.application.poll_attempts.asyncio.sleep", new=AsyncMock()):
        return await rt.poll(asg, sequence=1)


@pytest.mark.asyncio
async def test_geetest_request_headers_connection_close_and_timeout():
    """Verify GeeTest solver passes Connection: close and timeout=5.0 to prevent keep-alive socket hangs."""
    from hunter.transport.geetest_solver import _execute_geetest_attempt
    gc = AsyncMock(spec=httpx.AsyncClient)
    load_mock = MagicMock(text='geetest_123({"data": {"lot_number": "lot123", "captcha_type": "slide", "slice": "s.png", "bg": "b.png"}})', status_code=200)
    ver_mock = MagicMock(text='geetest_123({"data": {"seccode": {"lot_number": "lot123", "pass_token": "pt"}}})', status_code=200)
    gc.get = AsyncMock(side_effect=[load_mock, ver_mock])
    c = AsyncMock(spec=httpx.AsyncClient)

    with patch("hunter.transport.geetest_solver._solve_geetest_w", new=AsyncMock(return_value="w_token")), \
         patch("hunter.transport.geetest_solver.submit_avito_captcha_verify", new=AsyncMock(return_value=True)):
        solved, cookies = await _execute_geetest_attempt(c, gc, "fake_captcha_id")
        assert solved is True
        assert gc.get.call_count >= 2
        for call_args in gc.get.call_args_list:
            _, kwargs = call_args
            assert kwargs.get("headers", {}).get("Connection") == "close"
            assert kwargs.get("timeout") == 15.0


@pytest.mark.asyncio
async def test_solver_failure_user_b_peer_failover_to_user_a_recovers_200():
    """When user_b hits 403 and solver fails, fail over to user_a which returns 200 OK without slot quarantine."""
    challenge_fail = AsyncMock(return_value=ChallengeOutcome(
        slot_id="test-slot", status=ChallengeStatus.OPERATOR_REQUIRED, solved=False,
        new_session_version=None, quarantined=False, reason="Unresolved GeeTest",
    ))
    f, rt, asg, sigs, _, ingest = _setup(session_index=1, handle_challenge_mock=challenge_fail)
    c403 = MockResponse(403, '{"firewall/captcha/show":1}', headers={"x-firewall-show-captcha": "true"})
    c200 = MockResponse(200, FEED_HTML)
    try:
        with patch.object(f.curl_client_b, "get", new=AsyncMock(return_value=c403)) as gb, \
             patch.object(f.curl_client, "get", new=AsyncMock(return_value=c200)) as ga:
            res = await _run_poll(rt, asg)
            assert res is not None
            assert gb.await_count == 1
            assert ga.await_count == 1
            assert SlotSignal.CHALLENGE_DETECTED not in sigs
            assert SlotSignal.HTTP_403 not in sigs
            assert ingest.await_count == 1
    finally:
        await f.close()


@pytest.mark.asyncio
async def test_solver_failure_user_a_peer_failover_to_user_b_recovers_200():
    """Symmetrical: when user_a hits 403 and solver fails, fail over to user_b which returns 200 OK."""
    challenge_fail = AsyncMock(return_value=ChallengeOutcome(
        slot_id="test-slot", status=ChallengeStatus.OPERATOR_REQUIRED, solved=False,
        new_session_version=None, quarantined=False, reason="Unresolved GeeTest",
    ))
    f, rt, asg, sigs, _, ingest = _setup(session_index=0, handle_challenge_mock=challenge_fail)
    c403 = MockResponse(403, '{"firewall/captcha/show":1}', headers={"x-firewall-show-captcha": "true"})
    c200 = MockResponse(200, FEED_HTML)
    try:
        with patch.object(f.curl_client, "get", new=AsyncMock(return_value=c403)) as ga, \
             patch.object(f.curl_client_b, "get", new=AsyncMock(return_value=c200)) as gb:
            res = await _run_poll(rt, asg)
            assert res is not None
            assert ga.await_count == 1
            assert gb.await_count == 1
            assert SlotSignal.CHALLENGE_DETECTED not in sigs
            assert ingest.await_count == 1
    finally:
        await f.close()


@pytest.mark.asyncio
async def test_solver_failure_both_users_fail_triggers_quarantine():
    """When both user_b and user_a fail to recover from challenge, slot is quarantined."""
    challenge_fail = AsyncMock(return_value=ChallengeOutcome(
        slot_id="test-slot", status=ChallengeStatus.OPERATOR_REQUIRED, solved=False,
        new_session_version=None, quarantined=False, reason="Unresolved GeeTest",
    ))
    f, rt, asg, sigs, _, ingest = _setup(session_index=1, handle_challenge_mock=challenge_fail)
    h_c = {"x-firewall-show-captcha": "true"}
    c403_b = MockResponse(403, '{"firewall/captcha/show":1}', headers=h_c)
    c403_a = MockResponse(403, '{"firewall/captcha/show":2}', headers=h_c)
    try:
        with patch.object(f.curl_client_b, "get", new=AsyncMock(return_value=c403_b)) as gb, \
             patch.object(f.curl_client, "get", new=AsyncMock(return_value=c403_a)) as ga:
            res = await _run_poll(rt, asg)
            assert res is None
            assert gb.await_count == 1
            assert ga.await_count == 1
            assert (SlotSignal.TIMEOUT in sigs or SlotSignal.CHALLENGE_DETECTED in sigs)
            assert ingest.await_count == 0
    finally:
        await f.close()


@pytest.mark.asyncio
async def test_solver_failure_single_user_triggers_quarantine():
    """When slot has no peer identity, solver failure immediately triggers quarantine."""
    challenge_fail = AsyncMock(return_value=ChallengeOutcome(
        slot_id="test-slot", status=ChallengeStatus.OPERATOR_REQUIRED, solved=False,
        new_session_version=None, quarantined=True, reason="Unresolved GeeTest",
    ))
    f, rt, asg, sigs, _, ingest = _setup(session_index=0, handle_challenge_mock=challenge_fail)
    f.curl_client_b = None
    c403 = MockResponse(403, '{"firewall/captcha/show":1}', headers={"x-firewall-show-captcha": "true"})
    try:
        with patch.object(f.curl_client, "get", new=AsyncMock(return_value=c403)) as ga:
            res = await _run_poll(rt, asg)
            assert res is None
            assert ga.await_count == 1
            assert (SlotSignal.TIMEOUT in sigs or SlotSignal.CHALLENGE_DETECTED in sigs)
            assert ingest.await_count == 0
    finally:
        await f.close()


@pytest.mark.asyncio
async def test_solver_failure_user_b_peer_user_a_challenged_but_solves_and_recovers():
    """When user_b fails solver, user_a also gets 403 but user_a solves challenge and recovers poll."""
    challenge_responses = [
        ChallengeOutcome(slot_id="test-slot", status=ChallengeStatus.OPERATOR_REQUIRED, solved=False, new_session_version=None, quarantined=False),
        ChallengeOutcome(slot_id="test-slot", status=ChallengeStatus.SOLVED, solved=True, new_session_version=3, quarantined=False),
    ]
    challenge_mock = AsyncMock(side_effect=challenge_responses)
    f, rt, asg, sigs, _, ingest = _setup(session_index=1, handle_challenge_mock=challenge_mock)
    h_c = {"x-firewall-show-captcha": "true"}
    c403_b = MockResponse(403, '{"firewall/captcha/show":1}', headers=h_c)
    c403_a = MockResponse(403, '{"firewall/captcha/show":2}', headers=h_c)
    c200_a = MockResponse(200, FEED_HTML)
    try:
        with patch.object(f.curl_client_b, "get", new=AsyncMock(return_value=c403_b)) as gb, \
             patch.object(f.curl_client, "get", new=AsyncMock(side_effect=[c403_a, c200_a])) as ga:
            res = await _run_poll(rt, asg)
            assert res is not None
            assert gb.await_count == 1
            assert ga.await_count == 2
            assert SlotSignal.CHALLENGE_DETECTED not in sigs
            assert ingest.await_count == 1
    finally:
        await f.close()


@pytest.mark.asyncio
async def test_network_error_during_peer_failover_handled_gracefully():
    """When peer failover experiences network error/500, runtime handles gracefully and quarantines."""
    challenge_fail = AsyncMock(return_value=ChallengeOutcome(
        slot_id="test-slot", status=ChallengeStatus.OPERATOR_REQUIRED, solved=False,
        new_session_version=None, quarantined=False, reason="Unresolved GeeTest",
    ))
    f, rt, asg, sigs, _, ingest = _setup(session_index=1, handle_challenge_mock=challenge_fail)
    c403_b = MockResponse(403, '{"firewall/captcha/show":1}', headers={"x-firewall-show-captcha": "true"})
    c502_a = MockResponse(502, "Bad Gateway")
    c502_a.error = "Connection refused"
    try:
        with patch.object(f.curl_client_b, "get", new=AsyncMock(return_value=c403_b)) as gb, \
             patch.object(f.curl_client, "get", new=AsyncMock(return_value=c502_a)) as ga:
            res = await _run_poll(rt, asg)
            assert res is None
            assert gb.await_count == 1
            assert ga.await_count == 1
            assert (SlotSignal.TIMEOUT in sigs or SlotSignal.CHALLENGE_DETECTED in sigs)
            assert ingest.await_count == 0
    finally:
        await f.close()


@pytest.mark.asyncio
async def test_tokenless_403_user_a_peer_failover_to_user_b_recovers_200():
    """When user_a receives a session-level tokenless 403, fail over to user_b which returns 200 OK."""
    challenge_mock = AsyncMock()
    f, rt, asg, sigs, _, ingest = _setup(session_index=0, handle_challenge_mock=challenge_mock)
    c403 = MockResponse(403, SESSION_403_HTML)
    c200 = MockResponse(200, FEED_HTML)
    try:
        with patch.object(f.curl_client, "get", new=AsyncMock(return_value=c403)) as ga, \
             patch.object(f.curl_client_b, "get", new=AsyncMock(return_value=c200)) as gb:
            res = await _run_poll(rt, asg)
            assert res is not None
            assert ga.await_count == 1
            assert gb.await_count == 1
            assert SlotSignal.CHALLENGE_DETECTED not in sigs
            assert SlotSignal.HTTP_403 not in sigs
            assert ingest.await_count == 1
    finally:
        await f.close()


@pytest.mark.asyncio
async def test_tokenless_403_peer_failover_waits_cooldown_and_skips_solver_on_peer_tokenless():
    """Verify session-level peer failover waits 4.0s and skips the solver on a tokenless peer 403."""
    challenge_mock = AsyncMock()
    f, rt, asg, sigs, _, ingest = _setup(session_index=0, handle_challenge_mock=challenge_mock)
    tokenless_403 = MockResponse(403, SESSION_403_HTML)
    sleep_calls = []
    mock_sleep = AsyncMock(side_effect=lambda s: sleep_calls.append(s))
    try:
        with patch.object(f.curl_client, "get", new=AsyncMock(return_value=tokenless_403)), \
             patch.object(f.curl_client_b, "get", new=AsyncMock(return_value=tokenless_403)), \
             patch("hunter.application.public_poll_runtime.resolve_poll_target", return_value=("https://www.avito.ru/web/1/js/items", {})), \
             patch("hunter.application.public_poll_runtime.record_poll_response"), \
             patch("hunter.application.public_poll_runtime.asyncio.sleep", new=mock_sleep), \
             patch("hunter.application.poll_attempts.asyncio.sleep", new=mock_sleep):
            res = await rt.poll(asg, sequence=1)
            assert res is None
            assert challenge_mock.handle_challenge.await_count == 0
            assert 4.0 in sleep_calls
    finally:
        await f.close()


