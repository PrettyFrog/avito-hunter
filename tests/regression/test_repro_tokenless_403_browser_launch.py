"""Regression test: tokenless 403 barriers must not launch Camoufox browser."""

from pathlib import Path
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hunter.application.public_poll_runtime import PublicPollRuntime, QueryAssignment
from hunter.domain.poll_policy import PollPolicy
from hunter.domain.slots import SlotRole, SlotSignal
from hunter.storage.connection import StorageConfig, StorageConnectionManager
from hunter.storage.slot_state import SlotRepository
from hunter.storage.writer import SQLiteWriterActor
from hunter.transport.challenge import ChallengeHandoffService, ChallengeRequest
from hunter.transport.http_fetch import SlotFetchResponse
from hunter.transport.session_store import SessionStore

SCHEMA_PATH = Path(__file__).parents[2] / "src" / "hunter" / "core" / "schema.sql"


@pytest.fixture
async def challenge_env(tmp_path: Path):
    db_path = tmp_path / "test_pow_repro.db"
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    con.close()

    mgr = StorageConnectionManager(StorageConfig(db_path=db_path))
    writer = SQLiteWriterActor(mgr)
    await writer.start()
    repo = SlotRepository(mgr, writer)
    session_store = SessionStore(base_dir=tmp_path / "sessions")

    yield repo, session_store, mgr, writer

    await writer.stop()


@pytest.mark.asyncio
async def test_tokenless_403_does_not_launch_browser_in_challenge_service(challenge_env) -> None:
    repo, session_store, _, _ = challenge_env
    await repo.register_slot("slot-test", "direct://", role=SlotRole.ACTIVE)

    mock_browser = AsyncMock()
    service = ChallengeHandoffService(
        slot_repo=repo,
        session_store=session_store,
        browser_factory=mock_browser,
    )

    req = ChallengeRequest(
        slot_id="slot-test",
        challenge_type="pow",
        html="<html><head><title>Доступ ограничен: проблема с IP</title></head><body>WAF block</body></html>",
    )

    outcome = await service.handle_challenge(req)

    assert not mock_browser.called, "Browser must NOT be launched for tokenless 403 challenge"
    assert outcome.solved is False
    assert outcome.quarantined is True


@pytest.mark.asyncio
@pytest.mark.parametrize("status,body,headers,cookies,expected_handoff", [
    (403, "<html><head><title>Доступ ограничен: проблема с IP</title></head><body>WAF block</body></html>", {}, {}, False),
    (429, "<html><head><title>Доступ ограничен: проблема с IP</title></head><body>WAF block</body></html>", {}, {}, False),
    (403, "Доступ ограничен: проблема с IP", {"set-cookie": "pow_challenge=bounded-fake-token; Path=/"}, {}, True),
    (429, "Доступ ограничен: проблема с IP", {}, {"pow_challenge": "bounded-fake-token"}, True),
    (403, "<script>window.pow_challenge='bounded-fake-token'</script>", {}, {}, True),
    (439, "<html>firewall</html>", {}, {}, True),
    (403, '<div class="geetest_holder"></div>', {}, {}, True),
    (403, 'Доступ ограничен: проблема с IP <script src="/firewallPow.js"></script>', {}, {}, True),
])
async def test_poll_runtime_challenge_handoff_gating(status, body, headers, cookies, expected_handoff) -> None:
    response = SlotFetchResponse(
        status_code=status,
        url="https://www.avito.ru/moskva",
        text=body,
        headers=headers,
        cookies=cookies,
    )
    fetcher = SimpleNamespace(fetch=AsyncMock(return_value=response))
    pool = SimpleNamespace(get_fetcher=lambda *_: fetcher)
    challenge_service = SimpleNamespace(handle_challenge=AsyncMock(return_value=SimpleNamespace(solved=False)))
    signal_handler = AsyncMock()
    ingest = AsyncMock()

    runtime = PublicPollRuntime(
        None, pool, ingest, PollPolicy(),
        challenge_service=challenge_service,
        slot_signal_handler=signal_handler,
    )
    assignment = QueryAssignment("slot-test", "direct://", "q1", response.url, 1, 0)
    await runtime.poll(assignment, 1)

    assert challenge_service.handle_challenge.await_count == int(expected_handoff), (
        f"Expected handoff={expected_handoff} for status={status}, got {challenge_service.handle_challenge.await_count}"
    )
