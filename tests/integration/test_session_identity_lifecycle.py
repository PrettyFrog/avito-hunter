"""Real profile fencing for failed A/B handoff; no requests or docs fixtures."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from tests.fixtures.reference_storage import reference_storage as _reference_storage
from hunter.application.public_poll_runtime import PublicPollRuntime, QueryAssignment
from hunter.domain.poll_policy import PollPolicy
from hunter.domain.slots import SlotHealth
from hunter.transport.challenge import ChallengeStatus
from hunter.transport.http_fetch import SlotFetchResponse


async def profiles(repo, store):
    await repo.register_slot("audit", "direct")
    store.bind_reference_backend(repo.writer)
    for sid, value in (("audit", "healthy-a"), ("audit__user_b", "healthy-b")):
        await store.save_session(sid, "direct", 1, 1, cookies=[{"name": "u", "value": value}])


def poller(repo, store, outcome, *, effect=None):
    service = SimpleNamespace(slot_repo=repo, session_store=store,
                              handle_challenge=AsyncMock(return_value=outcome, side_effect=effect))
    fetcher = SimpleNamespace(reset_identity_in_memory=Mock(), load_cookies_from_store=AsyncMock())
    pool = SimpleNamespace(_fetchers={"audit": fetcher})
    runtime = PublicPollRuntime(None, pool, AsyncMock(), PollPolicy(),
                                challenge_service=service, identity_cleanup=True)
    return runtime, fetcher


def result(identity, reason):
    return SimpleNamespace(slot_id="audit", status=ChallengeStatus.OPERATOR_REQUIRED, solved=False,
                           client_identity=identity, failure_kind=reason, reason="synthetic")


async def handle(runtime, identity):
    assignment = QueryAssignment("audit", "direct", "q", "https://www.avito.ru/moskva?q=fixture", 1, 0)
    return await runtime._handle_challenge(assignment, SlotFetchResponse(439, "", "barrier", client_identity=identity))


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["transport_error", "unsupported", "recovery_failed", "unknown"])
async def test_uncertain_error_keeps_both_profiles(reference_storage, reason):
    repo, store = reference_storage
    await profiles(repo, store)
    before = [await store.load_session(sid) for sid in ("audit", "audit__user_b")]
    runtime, fetcher = poller(repo, store, result("user_b", reason))
    await handle(runtime, "user_b")
    after = [await store.load_session(sid) for sid in ("audit", "audit__user_b")]
    assert after == before
    fetcher.reset_identity_in_memory.assert_not_called()
    fetcher.load_cookies_from_store.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("identity,target,other", [
    ("user_b", "audit__user_b", "audit"), ("user_a", "audit", "audit__user_b"),
])
async def test_invalid_identity_is_deleted_without_touching_other(reference_storage, identity, target, other):
    repo, store = reference_storage
    await profiles(repo, store)
    before = await store.load_session(other)
    runtime, fetcher = poller(repo, store, result(identity, "invalid_profile"))
    await handle(runtime, identity)
    assert await store.load_session(target) is None
    assert await store.load_session(other) == before
    fetcher.reset_identity_in_memory.assert_called_once_with(identity)
    fetcher.load_cookies_from_store.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("origin,outcome_identity", [("", "user_b"), ("user_b", ""), ("user_b", "user_a")])
async def test_unknown_identity_does_not_guess_a(reference_storage, origin, outcome_identity):
    repo, store = reference_storage
    await profiles(repo, store)
    runtime, fetcher = poller(repo, store, result(outcome_identity, "invalid_profile"))
    await handle(runtime, origin)
    assert await store.load_session("audit") is not None
    assert await store.load_session("audit__user_b") is not None
    fetcher.reset_identity_in_memory.assert_not_called()


@pytest.mark.asyncio
async def test_late_invalid_outcome_does_not_delete_newer_profile(reference_storage):
    repo, store = reference_storage
    await profiles(repo, store)
    async def update_then_fail(request):
        await store.save_session("audit__user_b", "direct", 99, 99, cookies=[{"name": "u", "value": "winner"}])
        return result("user_b", "invalid_profile")
    runtime, fetcher = poller(repo, store, None, effect=update_then_fail)
    await handle(runtime, "user_b")
    surviving = await store.load_session("audit__user_b")
    assert surviving.metadata.version == 99 and surviving.cookies[0]["value"] == "winner"
    assert (await store.load_session("audit")).cookies[0]["value"] == "healthy-a"
    fetcher.reset_identity_in_memory.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["disabled", "generation"])
async def test_late_outcome_respects_slot_fence(reference_storage, change):
    repo, store = reference_storage
    await profiles(repo, store)
    async def change_then_fail(request):
        if change == "disabled":
            await repo.set_health("audit", SlotHealth.DISABLED)
        else:
            await repo.bump_generation("audit")
        return result("user_b", "invalid_profile")
    runtime, fetcher = poller(repo, store, None, effect=change_then_fail)
    await handle(runtime, "user_b")
    assert await store.load_session("audit__user_b") is not None
    assert await store.load_session("audit") is not None
    fetcher.reset_identity_in_memory.assert_not_called()


@pytest.mark.asyncio
async def test_cancel_after_delete_commit_still_resets_only_failed_memory(reference_storage, monkeypatch):
    import asyncio
    repo, store = reference_storage
    await profiles(repo, store)
    committed, release = asyncio.Event(), asyncio.Event()
    original = store.delete_session_if_matches
    async def delayed(*args, **kwargs):
        deleted = await original(*args, **kwargs)
        committed.set()
        await release.wait()
        return deleted
    monkeypatch.setattr(store, "delete_session_if_matches", delayed)
    runtime, fetcher = poller(repo, store, result("user_b", "invalid_profile"))
    task = asyncio.create_task(handle(runtime, "user_b"))
    await asyncio.wait_for(committed.wait(), 2)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await store.load_session("audit__user_b") is None
    assert (await store.load_session("audit")).cookies[0]["value"] == "healthy-a"
    fetcher.reset_identity_in_memory.assert_called_once_with("user_b")
