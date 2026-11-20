"""Regression test for PoW challenge resolution enforcement and immediate poll retry."""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
import sqlite3
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hunter.application.public_poll_runtime import PublicPollRuntime, QueryAssignment
from hunter.domain.observations import ParseOutcome
from hunter.domain.poll_policy import PollPolicy
from hunter.domain.slots import SlotCapacity, SlotHealth, SlotRole, SlotSignal
from hunter.storage.connection import StorageConfig, StorageConnectionManager
from hunter.storage.slot_state import SlotRepository
from hunter.storage.writer import SQLiteWriterActor
from hunter.transport.challenge import (
    ChallengeHandoffService,
    ChallengeOutcome,
    ChallengeRequest,
    ChallengeStatus,
)
from hunter.transport.session_store import SessionStore

SCHEMA_PATH = Path("src/hunter/core/schema.sql")


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
    await mgr.drain_and_close()


@pytest.mark.asyncio
async def test_prefilled_adcc_cookie_does_not_skip_solve_firewall_pow(challenge_env) -> None:
    repo, session_store, _, _ = challenge_env
    await repo.register_slot("slot_pow", "http://127.0.0.1:8080", role=SlotRole.ACTIVE)

    page = MagicMock()
    page.cookies = AsyncMock(return_value=[{"name": "_adcc", "value": "stale_unverified_cookie"}])
    page.close = AsyncMock()

    service = ChallengeHandoffService(
        slot_repo=repo,
        session_store=session_store,
        browser_factory=AsyncMock(return_value=page),
    )

    req = ChallengeRequest(
        slot_id="slot_pow",
        challenge_type="pow",
        html="<html><body><script>window.pow_challenge='fake_jwt';</script></body></html>",
    )

    with patch("hunter.transport.challenge.solve_firewall_pow", new=AsyncMock(return_value=True)) as mock_solver:
        outcome = await service.handle_challenge(req)

    assert mock_solver.called, "solve_firewall_pow must be called even if _adcc is in prefilled cookies"
    assert outcome.status == ChallengeStatus.SOLVED, f"Failed with: {outcome.reason}"
    assert outcome.solved is True


@pytest.mark.asyncio
async def test_subsequent_challenge_does_not_skip_solver_when_version_unchanged(challenge_env) -> None:
    repo, session_store, _, _ = challenge_env
    await repo.register_slot("slot_storm", "http://127.0.0.1:8080", role=SlotRole.ACTIVE)

    page = MagicMock()
    page.cookies = AsyncMock(return_value=[{"name": "pow_solved", "value": "1"}])
    page.close = AsyncMock()

    browser_factory_mock = AsyncMock(return_value=page)
    service = ChallengeHandoffService(
        slot_repo=repo,
        session_store=session_store,
        browser_factory=browser_factory_mock,
    )
    # Simulate a prior refresh recorded 5 seconds ago
    service._recent_refreshes["slot_storm"] = time.monotonic() - 5.0

    req = ChallengeRequest(
        slot_id="slot_storm",
        challenge_type="pow",
        html="<html><body><script>window.pow_challenge='another_jwt';</script></body></html>",
    )

    with patch("hunter.transport.challenge.solve_firewall_pow", new=AsyncMock(return_value=True)):
        outcome = await service.handle_challenge(req)

    # If the session version was not bumped by another worker, it should NOT return cached solved
    assert browser_factory_mock.called, "Browser factory must be invoked if session was not bumped while waiting"
    assert outcome.status == ChallengeStatus.SOLVED
    assert outcome.solved is True


@pytest.mark.asyncio
async def test_poll_retries_fetch_immediately_after_challenge_solved() -> None:
    challenge_service = MagicMock()
    challenge_service.handle_challenge = AsyncMock(
        return_value=ChallengeOutcome(
            slot_id="slot_1",
            status=ChallengeStatus.SOLVED,
            solved=True,
            new_session_version=2,
            quarantined=False,
        )
    )

    class MockResponse439:
        error = None
        status_code = 439
        text = "<html><body>pow_challenge=xyz</body></html>"
        url = "https://www.avito.ru/search?q=iphone"
        elapsed_sec = 0.05

    valid_html = """
    <div data-marker="catalog-serp">
      <div data-marker="item" data-item-id="8340205074">
        <a data-marker="item-title" href="/item/8340205074"><h3>iPhone 13</h3></a>
        <meta itemprop="price" content="50000" />
      </div>
    </div>
    """

    class MockResponse200:
        error = None
        status_code = 200
        text = valid_html
        url = "https://www.avito.ru/search?q=iphone"
        elapsed_sec = 0.1

    fetcher = MagicMock()
    fetcher.fetch = AsyncMock(side_effect=[MockResponse439(), MockResponse200()])
    fetcher.load_cookies_from_store = AsyncMock(return_value=1)

    http_pool = MagicMock()
    http_pool.get_fetcher.return_value = fetcher
    http_pool._fetchers = {"slot_1": fetcher}

    ingest_mock = AsyncMock()
    runtime = PublicPollRuntime(
        conn_mgr=None,
        http_pool=http_pool,
        ingest=ingest_mock,
        policy=PollPolicy(day_interval_sec=1, night_interval_sec=1),
        challenge_service=challenge_service,
    )

    assignment = QueryAssignment(
        slot_id="slot_1",
        query_id="q1",
        canonical_url="https://www.avito.ru/search?q=iphone",
        generation=1,
        endpoint="http://127.0.0.1:8080",
        position=0,
    )

    await runtime.poll(assignment, sequence=1)

    assert fetcher.fetch.call_count == 2, "Fetcher should retry immediately after challenge resolution"
    assert ingest_mock.called, "Ingest should be called with resolved batch"
    batch = ingest_mock.call_args[0][0]
    assert batch.outcome == ParseOutcome.OK, f"Expected ParseOutcome.OK after retry, got {batch.outcome}"
    assert len(batch.listings) == 1, "Listings from retried 200 response should be ingested"


def test_extract_pow_challenge_from_set_cookie_header() -> None:
    from hunter.collect.firewall_pow import extract_pow_challenge
    headers = {
        "set-cookie": "pow_challenge=osKdv85WvFOsIuA856_token123; path=/; domain=.avito.ru"
    }
    html = "<html><script>var challenge = getCookie('pow_challenge');</script></html>"
    token = extract_pow_challenge(html, headers=headers)
    assert token == "osKdv85WvFOsIuA856_token123"


def test_extract_pow_challenge_from_cookies_dict() -> None:
    from hunter.collect.firewall_pow import extract_pow_challenge
    cookies = {"pow_challenge": "token_from_cookies_456"}
    html = "<html><script>var challenge = getCookie('pow_challenge');</script></html>"
    token = extract_pow_challenge(html, cookies=cookies)
    assert token == "token_from_cookies_456"


@pytest.mark.asyncio
async def test_solve_firewall_pow_passes_extracted_header_token_to_evaluate() -> None:
    from hunter.collect.firewall_pow import solve_firewall_pow
    page = MagicMock()
    passed_args = []

    async def fake_evaluate(script, *args, **kwargs):
        if "firewallPow/get" in script:
            passed_args.append(args[0] if args else None)
            return {"success": True, "id": "id1", "compl": 3, "jwt": "h.eyJpZCI6ImlkMSIsImNvbXBsIjozfQ.s"}
        elif "firewallPow/verify" in script:
            return {"verified": True}
        return {}

    page.evaluate = AsyncMock(side_effect=fake_evaluate)
    headers = {"set-cookie": "pow_challenge=cookie_challenge_abc789; path=/"}
    html = "<html>var challenge = getCookie('pow_challenge');</html>"
    unblocked = await solve_firewall_pow(page, html=html, headers=headers)
    assert unblocked is True
    assert passed_args == ["cookie_challenge_abc789"]


@pytest.mark.asyncio
async def test_poll_passes_response_headers_and_cookie_challenge_to_service() -> None:
    challenge_service = MagicMock()
    captured_reqs: list[ChallengeRequest] = []

    async def fake_handle_challenge(req: ChallengeRequest) -> ChallengeOutcome:
        captured_reqs.append(req)
        return ChallengeOutcome(
            slot_id=req.slot_id,
            status=ChallengeStatus.SOLVED,
            solved=True,
            new_session_version=2,
            quarantined=False,
        )

    challenge_service.handle_challenge = AsyncMock(side_effect=fake_handle_challenge)

    class MockResponse439Cookie:
        error = None
        status_code = 439
        text = "<html><body>var challenge = getCookie('pow_challenge');</body></html>"
        url = "https://www.avito.ru/search?q=iphone"
        headers = {"set-cookie": "pow_challenge=token_from_header_999; Path=/"}
        cookies = {"pow_challenge": "token_from_header_999"}
        elapsed_sec = 0.05

    class MockResponse200Simple:
        error = None
        status_code = 200
        text = '<div data-marker="catalog-serp"><div data-marker="item" data-item-id="123"><a data-marker="item-title" href="/123"><h3>x</h3></a></div></div>'
        url = "https://www.avito.ru/search?q=iphone"
        headers = {}
        cookies = {}
        elapsed_sec = 0.05

    fetcher = MagicMock()
    fetcher.fetch = AsyncMock(side_effect=[MockResponse439Cookie(), MockResponse200Simple()])
    fetcher.load_cookies_from_store = AsyncMock(return_value=1)

    http_pool = MagicMock()
    http_pool.get_fetcher.return_value = fetcher
    http_pool._fetchers = {"slot_1": fetcher}

    runtime = PublicPollRuntime(
        conn_mgr=None,
        http_pool=http_pool,
        ingest=AsyncMock(),
        policy=PollPolicy(day_interval_sec=1, night_interval_sec=1),
        challenge_service=challenge_service,
    )

    assignment = QueryAssignment(
        slot_id="slot_1", query_id="q1", canonical_url="https://www.avito.ru/search?q=iphone",
        generation=1, endpoint="http://127.0.0.1:8080", position=0,
    )
    await runtime.poll(assignment, sequence=1)

    assert len(captured_reqs) == 1
    req = captured_reqs[0]
    assert getattr(req, "challenge_token", None) == "token_from_header_999"


@pytest.mark.asyncio
async def test_solve_firewall_pow_http_flow() -> None:
    from hunter.collect.firewall_pow import solve_firewall_pow_http
    payload = {"id": "chal_http_1", "compl": 2}
    jwt_p = base64.b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    fake_jwt = f"hdr.{jwt_p}.sig"
    client = MagicMock()
    async def mock_post(url: str, **kwargs):
        if "/web/3/firewallPow/get" in url:
            m = MagicMock(status_code=200, headers={"Set-Cookie": "srv_id=srv999; Path=/"})
            m.json.return_value = {"success": {"result": {"challenge_jwt": fake_jwt}}}
            return m
        elif "/web/3/firewallPow/verify" in url:
            m = MagicMock(status_code=200, headers={"Set-Cookie": "_adcc=adcc_val_123; Path=/"})
            m.json.return_value = {"success": {"result": {"verified": True}}}
            return m
        return MagicMock(status_code=404)
    client.post = AsyncMock(side_effect=mock_post)
    solved, cookies = await solve_firewall_pow_http("tok_abc", cookies={"_avisc": "val1"}, http_client=client)
    assert solved is True
    cd = {c["name"]: c["value"] for c in cookies}
    assert cd.get("_adcc") == "adcc_val_123" and cd.get("_avisc") == "val1" and cd.get("pow_solved") == "1"


@pytest.mark.asyncio
async def test_solve_firewall_pow_http_handles_rejections() -> None:
    from hunter.collect.firewall_pow import solve_firewall_pow_http
    client = MagicMock()
    client.post = AsyncMock(return_value=MagicMock(status_code=403))
    solved, cookies = await solve_firewall_pow_http("tok_bad", http_client=client)
    assert solved is False and cookies == []


@pytest.mark.asyncio
async def test_challenge_handoff_fast_path_http_without_browser(challenge_env) -> None:
    repo, session_store, _, _ = challenge_env
    await repo.register_slot("slot_http", "direct", role=SlotRole.ACTIVE)
    await repo.set_health("slot_http", SlotHealth.CHALLENGE)
    await session_store.save_session("slot_http", "direct", 1, 1, [{"name": "_avisc", "value": "old_avisc"}])

    mock_browser = AsyncMock()
    service = ChallengeHandoffService(slot_repo=repo, session_store=session_store, browser_factory=mock_browser)
    fake_cookies = [{"name": "_adcc", "value": "fresh_adcc", "domain": ".avito.ru", "path": "/"}]
    with patch("hunter.transport.challenge.solve_firewall_pow_http", new=AsyncMock(return_value=(True, fake_cookies))) as mock_solver:
        req = ChallengeRequest(slot_id="slot_http", challenge_type="pow", challenge_token="tok_123")
        outcome = await service.handle_challenge(req)

    assert mock_solver.called
    assert not mock_browser.called, "Browser factory MUST NOT be called when HTTP solve succeeds"
    assert outcome.status == ChallengeStatus.SOLVED and outcome.solved is True
    slot = await repo.get_slot("slot_http")
    assert slot is not None and slot.health == SlotHealth.READY and slot.session_version == 2
    sess = await session_store.load_session("slot_http")
    assert any(c.get("name") == "_adcc" and c.get("value") == "fresh_adcc" for c in sess.cookies)

