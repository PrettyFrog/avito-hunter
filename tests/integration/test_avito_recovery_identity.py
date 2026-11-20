import asyncio, sqlite3, pytest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from hunter.application.public_poll_runtime import PublicPollRuntime, QueryAssignment
from hunter.domain.poll_policy import PollPolicy
from hunter.storage.connection import StorageConfig, StorageConnectionManager
from hunter.storage.slot_state import SlotRepository
from hunter.storage.writer import SQLiteWriterActor
from hunter.transport.challenge import ChallengeHandoffService, ChallengeRequest
from hunter.transport.http_fetch import HttpSlotFetcher, SlotFetchConfig, SlotFetchResponse
from hunter.transport.session_store import SessionStore

@pytest.fixture
async def env(tmp_path):
    db = tmp_path / "t.db"
    with sqlite3.connect(db) as c: c.executescript(Path("src/hunter/core/schema.sql").read_text())
    mgr, store = StorageConnectionManager(StorageConfig(db_path=db)), SessionStore(str(tmp_path / "sessions"))
    writer = SQLiteWriterActor(mgr)
    await writer.start()
    try: yield SlotRepository(mgr, writer), store
    finally:
        await writer.stop()
        await mgr.drain_and_close()

@pytest.mark.asyncio
async def test_challenge_recovery_respects_user_b_identity_and_preserves_user_a(env):
    repo, store = env
    slot = await repo.register_slot("audit", "direct")
    await store.save_session("audit", "direct", 1, 1, cookies=[{"name": "only_A", "value": "audit-A"}, {"name": "u", "value": "A"}])
    await store.save_session("audit__user_b", "direct", 1, 1, cookies=[{"name": "only_B", "value": "audit-B"}, {"name": "u", "value": "B"}])
    raw_a_before = store._slot_path("audit").read_bytes()

    solver = AsyncMock(return_value=(True, [{"name": "u", "value": "B-new", "domain": ".avito.ru"}]))
    service = ChallengeHandoffService(repo, store, http_pow_solver=solver)

    req = ChallengeRequest("audit", client_identity="user_b", cookies={"pow_challenge": "token-b"})
    out = await service._attempt_http_pow(slot, req, "token-b")
    assert out and out.solved is True

    sent_cookie_names = {c["name"] for c in solver.await_args.kwargs["cookies"]}
    assert "only_B" in sent_cookie_names
    assert "only_A" not in sent_cookie_names

    # User A file must be byte-for-byte unchanged!
    assert store._slot_path("audit").read_bytes() == raw_a_before

    # User B session must have B-new
    b_sess = await store.load_session("audit__user_b")
    assert any(c["name"] == "u" and c["value"] == "B-new" for c in b_sess.cookies)

@pytest.mark.asyncio
async def test_http_fetch_marks_response_client_identity():
    fetcher = HttpSlotFetcher("audit", endpoint="direct", config=SlotFetchConfig(interleave_sessions=True))
    try:
        class FakeResp:
            status_code = 200
            url = "https://www.avito.ru/moskva"
            headers = {}
            cookies = {}
            encoding = "utf-8"
            astream_task = None
            async def aiter_content(self): yield b'{"catalog":{"items":[]}}'
            async def aclose(self): pass

        with patch.object(fetcher.curl_client, "get", AsyncMock(return_value=FakeResp())):
            fetcher._session_index = 0
            resp_a = await fetcher.fetch("https://www.avito.ru/web/1/js/items")
            assert getattr(resp_a, "client_identity", "") in ("user_a", "a", "")

        with patch.object(fetcher.curl_client_b, "get", AsyncMock(return_value=FakeResp())):
            fetcher._session_index = 1
            resp_b = await fetcher.fetch("https://www.avito.ru/web/1/js/items")
            assert getattr(resp_b, "client_identity", "") in ("user_b", "b")
    finally:
        await fetcher.close()


@pytest.mark.asyncio
async def test_public_poll_runtime_propagates_client_identity_to_challenge():
    svc = SimpleNamespace(handle_challenge=AsyncMock(return_value=SimpleNamespace(solved=True, status="solved")))
    fetcher = SimpleNamespace(
        curl_client_b=SimpleNamespace(headers={"User-Agent": "agent-B"}),
        proxy_credentials=("user", "pass"),
        load_cookies_from_store=AsyncMock(return_value=0),
    )
    pool = SimpleNamespace(get_fetcher=lambda *a: fetcher, _fetchers={"audit": fetcher})
    rt = PublicPollRuntime(None, pool, AsyncMock(), PollPolicy(), challenge_service=svc)
    resp = SlotFetchResponse(status_code=439, url="https://www.avito.ru", text='{"pow_challenge":"token"}', client_identity="user_b")
    await rt._handle_challenge("audit", resp)
    req = svc.handle_challenge.await_args.args[0]
    assert req.client_identity == "user_b"
    assert req.http_client is fetcher.curl_client_b
    assert req.headers.get("User-Agent") == "agent-B"
    assert req.proxy_credentials == ("user", "pass")


@pytest.mark.asyncio
async def test_public_poll_runtime_preserves_older_assignment_recovery_under_newer_slot_generation(env):
    repo, store = env
    await repo.register_slot("audit", "direct")
    gen1 = await repo.assign_query("audit", "q1")
    await repo.assign_query("audit", "q2")
    slot = await repo.get_slot("audit")
    assert slot.generation > gen1

    solver = AsyncMock(return_value=(True, [{"name": "u", "value": "audit-val", "domain": ".avito.ru"}]))
    service = ChallengeHandoffService(repo, store, http_pow_solver=solver)
    rt = PublicPollRuntime(None, None, AsyncMock(), PollPolicy(), challenge_service=service)

    a = QueryAssignment("audit", "direct", "q1", "https://www.avito.ru/q1", gen1, 0, slot_generation=slot.generation)
    resp = SlotFetchResponse(status_code=439, url="https://www.avito.ru", text='{"pow_challenge":"token"}', client_identity="user_a")
    out = await rt._handle_challenge(a, resp)

    assert out is not None and out.solved is True
    assert store.has_session("audit")
    sess = await store.load_session("audit")
    assert any(c["name"] == "u" and c["value"] == "audit-val" for c in sess.cookies)


@pytest.mark.asyncio
async def test_immutable_request_headers_snapshot_preserved_on_challenge_handoff():
    svc = SimpleNamespace(handle_challenge=AsyncMock(return_value=SimpleNamespace(solved=True, status="solved")))
    fetcher = SimpleNamespace(
        curl_client_b=SimpleNamespace(headers={"User-Agent": "agent-B-before"}),
        proxy_credentials=("user", "pass"),
        load_cookies_from_store=AsyncMock(return_value=0),
    )
    pool = SimpleNamespace(get_fetcher=lambda *a: fetcher, _fetchers={"audit": fetcher})
    rt = PublicPollRuntime(None, pool, AsyncMock(), PollPolicy(), challenge_service=svc)

    async def fake_fetch(url, headers=None, client_identity=None):
        fetcher.curl_client_b.headers["User-Agent"] = "agent-B-after"
        return SlotFetchResponse(
            status_code=439, url="https://www.avito.ru", text='{"pow_challenge":"token"}',
            client_identity="user_b",
        )

    fetcher.fetch = fake_fetch
    from hunter.application.poll_attempts import safe_fetch_attempt
    resp = await safe_fetch_attempt(fetcher, "https://www.avito.ru", {"X-Custom": "val"}, client_identity="user_b")
    a = QueryAssignment("audit", "direct", "q1", "https://www.avito.ru/q1", 1, 0)
    await rt._handle_challenge(a, resp)

    req = svc.handle_challenge.await_args.args[0]
    assert req.headers.get("User-Agent") == "agent-B-before"
    assert req.headers.get("X-Custom") == "val"


@pytest.mark.asyncio
async def test_case_insensitive_origin_headers_snapshot_no_duplicate_ua():
    import httpx

    fetcher = None

    async def handler(request):
        assert fetcher is not None
        fetcher.client.headers["User-Agent"] = "synthetic-UA-after"
        return httpx.Response(439, text='{"pow_challenge":"synthetic"}')

    fetcher = HttpSlotFetcher(
        "audit", "direct", transport=httpx.MockTransport(handler),
        config=SlotFetchConfig(interleave_sessions=False),
    )
    try:
        fetcher.client.headers["User-Agent"] = "synthetic-UA-before"
        response = await fetcher.fetch("https://www.avito.ru/moskva?q=synthetic")
        from hunter.application.poll_attempts import prepare_challenge_request
        a = QueryAssignment("audit", "direct", "q_audit", "https://www.avito.ru/moskva?q=synthetic", 1, 0)
        request = await prepare_challenge_request(a, response, SimpleNamespace(_fetchers={"audit": fetcher}), None)

        ua_values = [v for k, v in request.headers.items() if k.lower() == "user-agent"]
        assert ua_values == ["synthetic-UA-before"]
        assert request.headers.get("User-Agent") == "synthetic-UA-before"
        assert request.headers.get("user-agent") == "synthetic-UA-before"
    finally:
        await fetcher.close()

