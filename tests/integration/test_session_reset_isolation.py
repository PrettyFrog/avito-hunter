"""In-memory reset may change only the explicitly failed identity, without HTTP."""
import httpx
import pytest
from tests.fixtures.reference_storage import reference_storage as _reference_storage

from hunter.transport.http_fetch import HttpSlotFetcher


class MemoryClient:
    def __init__(self, value):
        self.cookies = httpx.Cookies()
        self.cookies.set("u", value, domain=".avito.ru", path="/")
        self.headers = {"User-Agent": value + "-ua", "X-Keep": value}

    async def close(self):
        return None


def state(client):
    return ([(c.name, c.value, c.domain, c.path) for c in client.cookies.jar], dict(client.headers))


@pytest.fixture
async def fetcher():
    def unexpected(request):
        raise AssertionError("Memory reset must not issue HTTP")
    fetcher = HttpSlotFetcher("audit", "direct", transport=httpx.MockTransport(unexpected))
    fetcher.client.cookies.set("u", "httpx-a", domain=".avito.ru", path="/")
    fetcher.curl_client = MemoryClient("curl-a")
    fetcher.curl_client_b = MemoryClient("curl-b")
    fetcher._user_b_ready = True
    try:
        yield fetcher
    finally:
        await fetcher.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", ["user_a", "user_b"])
async def test_reset_preserves_other_identity_cookie_jar_and_user_agent(fetcher, identity):
    before_a = (state(fetcher.client), state(fetcher.curl_client))
    before_b = state(fetcher.curl_client_b)
    fetcher.reset_identity_in_memory(identity)
    if identity == "user_b":
        assert (state(fetcher.client), state(fetcher.curl_client)) == before_a
        assert len(fetcher.curl_client_b.cookies) == 0
        assert fetcher._user_b_ready is False
    else:
        assert state(fetcher.curl_client_b) == before_b
        assert len(fetcher.client.cookies) == len(fetcher.curl_client.cookies) == 0
        assert fetcher._user_b_ready is True


@pytest.mark.asyncio
async def test_unknown_identity_does_not_default_to_a(fetcher):
    before = (state(fetcher.client), state(fetcher.curl_client), state(fetcher.curl_client_b))
    with pytest.raises(ValueError):
        fetcher.reset_identity_in_memory("")
    assert before == (state(fetcher.client), state(fetcher.curl_client), state(fetcher.curl_client_b))


@pytest.mark.asyncio
async def test_fresh_b_save_advances_past_tombstone(fetcher, reference_storage):
    repo, store = reference_storage
    store.bind_reference_backend(repo.writer)
    fetcher.session_store = store
    await store.save_session("audit__user_b", "direct", 5, 1, cookies=[{"name": "u", "value": "old"}])
    assert await store.delete_session("audit__user_b")
    await fetcher.save_user_b_cookies_to_store()
    profile = await store.load_session("audit__user_b")
    assert profile.metadata.version == 5 and profile.metadata.generation > 2
    assert profile.cookies[0]["value"] == "curl-b"


@pytest.mark.asyncio
async def test_warmup_cannot_overwrite_newer_b_winner(fetcher, reference_storage, monkeypatch):
    repo, store = reference_storage
    store.bind_reference_backend(repo.writer)
    fetcher.session_store, fetcher._user_b_ready = store, False
    await store.save_session("audit__user_b", "direct", 1, 1, cookies=[{"name": "u", "value": "old"}])
    before_a = state(fetcher.client), state(fetcher.curl_client)
    async def warmup(*args, **kwargs):
        await store.save_session("audit__user_b", "direct", 99, 99,
                                 cookies=[{"name": "u", "value": "winner"}])
        return True
    monkeypatch.setattr("hunter.transport.catalog_api.warmup_client_session", warmup)
    assert await fetcher._warmup_b_if_needed(fetcher.curl_client_b, "https://www.avito.ru/") is False
    assert (await store.load_session("audit__user_b")).metadata.version == 99
    assert fetcher.curl_client_b.cookies.get("u") == "winner"
    assert fetcher._user_b_ready is False
    assert (state(fetcher.client), state(fetcher.curl_client)) == before_a


@pytest.mark.asyncio
async def test_deleted_b_is_not_resurrected_from_old_memory_on_warmup(fetcher, reference_storage, monkeypatch):
    repo, store = reference_storage
    store.bind_reference_backend(repo.writer)
    fetcher.session_store, fetcher._user_b_ready = store, False
    await store.save_session("audit__user_b", "direct", 2, 1, cookies=[{"name": "u", "value": "old"}])
    assert await store.delete_session("audit__user_b")
    before_a = state(fetcher.client), state(fetcher.curl_client)
    async def warmup(client, *args, **kwargs):
        assert len(client.cookies) == 0
        return False
    monkeypatch.setattr("hunter.transport.catalog_api.warmup_client_session", warmup)
    assert await fetcher._warmup_b_if_needed(fetcher.curl_client_b, "https://www.avito.ru/") is False
    assert await store.load_session("audit__user_b") is None
    assert (state(fetcher.client), state(fetcher.curl_client)) == before_a


@pytest.mark.asyncio
async def test_runtime_deletes_b_and_clears_only_real_b_client(fetcher, reference_storage):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from hunter.application.public_poll_runtime import PublicPollRuntime, QueryAssignment
    from hunter.domain.poll_policy import PollPolicy
    from hunter.transport.challenge import ChallengeStatus
    from hunter.transport.http_fetch import SlotFetchResponse
    repo, store = reference_storage
    await repo.register_slot("audit", "direct")
    store.bind_reference_backend(repo.writer)
    fetcher.session_store = store
    for sid in ("audit", "audit__user_b"):
        await store.save_session(sid, "direct", 1, 1, cookies=[{"name": "u", "value": sid}])
    before_memory = state(fetcher.client), state(fetcher.curl_client)
    before_profile = await store.load_session("audit")
    outcome = SimpleNamespace(slot_id="audit", status=ChallengeStatus.OPERATOR_REQUIRED,
                              solved=False, client_identity="user_b", failure_kind="invalid_profile")
    service = SimpleNamespace(slot_repo=repo, session_store=store,
                              handle_challenge=AsyncMock(return_value=outcome))
    runtime = PublicPollRuntime(None, SimpleNamespace(_fetchers={"audit": fetcher}), AsyncMock(),
                                PollPolicy(), challenge_service=service, identity_cleanup=True)
    assignment = QueryAssignment("audit", "direct", "q", "https://www.avito.ru/moskva?q=fixture", 1, 0)
    await runtime._handle_challenge(assignment, SlotFetchResponse(439, "", "barrier", client_identity="user_b"))
    assert await store.load_session("audit__user_b") is None
    assert await store.load_session("audit") == before_profile
    assert len(fetcher.curl_client_b.cookies) == 0 and fetcher._user_b_ready is False
    assert (state(fetcher.client), state(fetcher.curl_client)) == before_memory


@pytest.mark.asyncio
async def test_successful_b_recovery_does_not_reload_healthy_a(fetcher, reference_storage, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from hunter.application.public_poll_runtime import PublicPollRuntime
    from hunter.domain.poll_policy import PollPolicy
    from hunter.transport.challenge import ChallengeHandoffService
    from hunter.transport.http_fetch import SlotFetchResponse
    repo, store = reference_storage
    await repo.register_slot("audit", "direct")
    store.bind_reference_backend(repo.writer)
    fetcher.session_store = store
    for sid in ("audit", "audit__user_b"):
        await store.save_session(sid, "direct", 1, 1, cookies=[{"name": "u", "value": sid}])
    before_a = state(fetcher.client), state(fetcher.curl_client)
    solver = AsyncMock(return_value=(True, [{"name": "u", "value": "solved-b", "domain": ".avito.ru", "path": "/"}]))
    service = ChallengeHandoffService(repo, store, http_pow_solver=solver, allow_browser=False)
    runtime = PublicPollRuntime(None, SimpleNamespace(_fetchers={"audit": fetcher}), AsyncMock(),
                                PollPolicy(), challenge_service=service, identity_cleanup=True)
    monkeypatch.setattr("hunter.collect.firewall_pow.extract_pow_challenge", lambda *args, **kwargs: "synthetic")
    outcome = await runtime._handle_challenge("audit", SlotFetchResponse(439, "", "barrier", client_identity="user_b"))
    assert outcome.solved
    assert (await store.load_session("audit__user_b")).cookies[0]["value"] == "solved-b"
    assert fetcher.curl_client_b.cookies.get("u") == "solved-b"
    assert (state(fetcher.client), state(fetcher.curl_client)) == before_a


@pytest.mark.asyncio
async def test_startup_reload_keeps_cookie_paths_expiry_and_drops_removed_memory(fetcher, reference_storage):
    import time
    repo, store = reference_storage
    store.bind_reference_backend(repo.writer)
    fetcher.session_store = store
    expiry = int(time.time()) + 1800
    cookies = [{"name": "k", "value": value, "domain": ".avito.ru", "path": path, "expires": expiry}
               for value, path in (("root", "/"), ("special", "/special"))]
    for sid in ("audit", "audit__user_b"):
        await store.save_session(sid, "direct", 1, 1, cookies=cookies)
    assert await fetcher.load_cookies_from_store() == 2
    for client in (fetcher.client, fetcher.curl_client, fetcher.curl_client_b):
        loaded = {(cookie.name, cookie.path): (cookie.value, cookie.expires) for cookie in client.cookies.jar}
        assert loaded == {("k", "/"): ("root", expiry), ("k", "/special"): ("special", expiry)}


@pytest.mark.asyncio
async def test_b_warmup_on_explicit_new_endpoint_never_uses_old_endpoint_tokens(fetcher, reference_storage, monkeypatch):
    repo, store = reference_storage
    store.bind_reference_backend(repo.writer)
    fetcher.session_store, fetcher._user_b_ready = store, False
    await store.save_session("audit__user_b", "http://old-proxy.invalid:8080", 2, 1,
                             cookies=[{"name": "u", "value": "old-route-token"}],
                             storage_state={"user_agent": "old-route-ua"})
    before_a = state(fetcher.client), state(fetcher.curl_client)
    async def warmup(client, *args, **kwargs):
        assert len(client.cookies) == 0
        assert client.headers["User-Agent"] != "old-route-ua"
        client.cookies.set("u", "new-route-token", domain=".avito.ru", path="/")
        return True
    monkeypatch.setattr("hunter.transport.catalog_api.warmup_client_session", warmup)
    assert await fetcher._warmup_b_if_needed(fetcher.curl_client_b, "https://www.avito.ru/")
    profile = await store.load_session("audit__user_b", expected_endpoint="direct")
    assert profile.cookies[0]["value"] == "new-route-token"
    assert profile.metadata.version == 2 and profile.metadata.generation == 2
    assert (state(fetcher.client), state(fetcher.curl_client)) == before_a


@pytest.mark.asyncio
async def test_failed_warmup_retains_partial_b_cookies_while_store_fence_is_unchanged(fetcher, reference_storage, monkeypatch):
    repo, store = reference_storage
    store.bind_reference_backend(repo.writer)
    fetcher.session_store, fetcher._user_b_ready = store, False
    await store.save_session("audit__user_b", "direct", 1, 1,
                             cookies=[{"name": "u", "value": "stored-profile"}])
    before_a = state(fetcher.client), state(fetcher.curl_client)
    calls = []
    async def warmup(client, *args, **kwargs):
        if calls:
            assert client.cookies.get("partial") == "retained"
        else:
            assert client.cookies.get("u") == "stored-profile"
            client.cookies.set("partial", "retained", domain=".avito.ru", path="/")
        calls.append(1)
        return False
    monkeypatch.setattr("hunter.transport.catalog_api.warmup_client_session", warmup)
    for _ in range(2):
        assert await fetcher._warmup_b_if_needed(fetcher.curl_client_b, "https://www.avito.ru/") is False
    assert calls == [1, 1]
    assert (state(fetcher.client), state(fetcher.curl_client)) == before_a
    assert (await store.load_session("audit__user_b")).metadata.generation == 1
