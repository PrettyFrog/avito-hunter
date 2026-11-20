"""Unit tests for secondary retry failure signaling and bounded recovery (T-972)."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import pytest

from hunter.application.public_poll_runtime import PublicPollRuntime, QueryAssignment
from hunter.domain.poll_policy import PollPolicy
from hunter.domain.slots import SlotSignal
from hunter.transport.http_fetch import HttpSlotFetcher, SlotFetchConfig


class MockResponse:
    def __init__(self, status: int, text: str = ""):
        self.status_code, self.text, self.body, self.headers = status, text, text, {}
        self.cookies, self.url, self.encoding, self.astream_task, self.error = {}, "https://www.avito.ru/moskva", "utf-8", None, None

    async def aiter_content(self): yield self.body.encode()
    async def aclose(self): pass

def _setup():
    f = HttpSlotFetcher("test-slot", endpoint="direct", config=SlotFetchConfig(interleave_sessions=True))
    f._session_index, f._user_b_ready, sigs, ingest = 1, True, [], AsyncMock()
    svc = SimpleNamespace(handle_challenge=AsyncMock(return_value=SimpleNamespace(solved=True)))
    rt = PublicPollRuntime(
        None, SimpleNamespace(get_fetcher=lambda *a: f, _fetchers={"test-slot": f}),
        ingest, PollPolicy(), slot_signal_handler=AsyncMock(side_effect=lambda slot_id, sig, **kw: sigs.append(sig)),
        challenge_service=svc,
    )
    return f, rt, QueryAssignment("test-slot", "direct", "q-1", "https://www.avito.ru/moskva/tovary", 1, 0), sigs, svc, ingest

async def _run(rt, f, asg, side_effect):
    last = side_effect[-1] if isinstance(side_effect, list) else side_effect
    a_kw = {"side_effect": last} if isinstance(last, Exception) else {"return_value": last}
    with patch.object(f.curl_client_b, "get", new=AsyncMock(side_effect=side_effect)), \
         patch.object(f.curl_client, "get", new=AsyncMock(**a_kw)), \
         patch("hunter.application.public_poll_runtime.resolve_poll_target", return_value=("https://www.avito.ru/web/1/js/items", {})), \
         patch("hunter.application.public_poll_runtime.record_poll_response"), \
         patch("hunter.application.public_poll_runtime.asyncio.sleep", new=AsyncMock()):
        return await rt.poll(asg, sequence=1)

@pytest.mark.asyncio
async def test_secondary_429_reports_signal_and_rejects_ingest():
    f, rt, asg, sigs, _, ingest = _setup()
    try:
        res = await _run(rt, f, asg, [MockResponse(439, '{"pow_challenge":"t1"}'), MockResponse(429, "Too Many Requests")])
        assert res is None and SlotSignal.HTTP_429 in sigs and ingest.await_count == 0
    finally: await f.close()

@pytest.mark.asyncio
async def test_secondary_timeout_reports_signal_and_rejects_ingest():
    f, rt, asg, sigs, _, ingest = _setup()
    try:
        res = await _run(rt, f, asg, [MockResponse(439, '{"pow_challenge":"t1"}'), Exception("timeout")])
        assert res is None and SlotSignal.TIMEOUT in sigs and ingest.await_count == 0
    finally: await f.close()

@pytest.mark.asyncio
async def test_secondary_403_reports_signal_and_rejects_ingest():
    f, rt, asg, sigs, _, ingest = _setup()
    try:
        res = await _run(rt, f, asg, [MockResponse(439, '{"pow_challenge":"t1"}'), MockResponse(403, "Forbidden")])
        assert res is None and SlotSignal.HTTP_403 in sigs and ingest.await_count == 0
    finally: await f.close()

@pytest.mark.asyncio
async def test_secondary_challenge_does_not_solve_recursively():
    f, rt, asg, sigs, svc, ingest = _setup()
    try:
        res = await _run(rt, f, asg, [MockResponse(439, '{"pow_challenge":"t1"}'), MockResponse(439, '{"pow_challenge":"t2"}')])
        assert res is None and svc.handle_challenge.await_count <= 2 and SlotSignal.CHALLENGE_DETECTED in sigs and ingest.await_count == 0
    finally: await f.close()
