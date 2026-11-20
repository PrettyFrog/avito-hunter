"""One owned recovery operation commits B once despite a cancelled subscriber."""
import asyncio
import sqlite3
from pathlib import Path

import pytest

from hunter.core.egress_config import EgressRegulatorConfig
from hunter.domain.request_budget import RequestBudget
from hunter.storage.connection import StorageConfig, StorageConnectionManager
from hunter.storage.slot_state import SlotRepository
from hunter.storage.writer import SQLiteWriterActor
from hunter.transport.challenge import ChallengeHandoffService, ChallengeRequest
from hunter.transport.metered_http import RequestGateway
from hunter.transport.session_store import SessionStore


async def _setup_env(tmp_path, db_name="singleflight.db"):
    path = tmp_path / db_name
    with sqlite3.connect(path) as conn:
        conn.executescript(Path("src/hunter/core/schema.sql").read_text())
    mgr = StorageConnectionManager(StorageConfig(db_path=path))
    writer = SQLiteWriterActor(mgr)
    await writer.start()
    store = SessionStore(str(tmp_path / f"sessions_{db_name}"))
    repo = SlotRepository(mgr, writer)
    slot = await repo.register_slot("audit", "direct")
    await store.save_session("audit", "direct", 1, 1,
                             cookies=[{"name": "u", "value": "A", "domain": ".avito.ru"}])
    return mgr, writer, store, repo, slot


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_duplicate_or_cancel_user_b_commit(tmp_path):
    mgr, writer, store, repo, slot = await _setup_env(tmp_path, "test1.db")
    service = None
    started, release, calls = asyncio.Event(), asyncio.Event(), []
    try:
        async def solver(**kwargs):
            calls.append(kwargs["http_client"])
            started.set()
            await release.wait()
            value = "B-new" if len(calls) == 1 else "A-new"
            return True, [{"name": "u", "value": value, "domain": ".avito.ru"}]
        gateway = RequestGateway(RequestBudget(enforce=True, spacing_sec=7.5))
        service = ChallengeHandoffService(repo, store, http_pow_solver=solver,
            allow_browser=False, egress_gateway=gateway, egress_groups=EgressRegulatorConfig())
        req = ChallengeRequest("audit", challenge_token="synthetic-token", client_identity="user_b",
                               expected_generation=slot.generation, endpoint="direct")
        first = asyncio.create_task(service.handle_challenge(req))
        await started.wait()
        second = asyncio.create_task(service.handle_challenge(req))
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        release.set()
        result = await second
        assert result.solved and len(calls) == 1 and calls[0].is_closed
        assert (await store.load_session("audit")).cookies[0]["value"] == "A"
        assert any(c["value"] == "B-new" for c in (await store.load_session("audit__user_b")).cookies)
        current = await repo.get_slot("audit")
        a_req = ChallengeRequest("audit", challenge_token="synthetic-token", client_identity="user_a",
                                 expected_generation=current.generation, endpoint="direct")
        assert (await service.handle_challenge(a_req)).solved
        assert len(calls) == 2
        assert any(c["value"] == "A-new" for c in (await store.load_session("audit")).cookies)
        assert any(c["value"] == "B-new" for c in (await store.load_session("audit__user_b")).cookies)
    finally:
        release.set()
        if service is not None:
            await service.close()
        await writer.stop()
        await mgr.drain_and_close()


@pytest.mark.asyncio
async def test_challenge_shutdown_cancels_and_drains_without_late_http(tmp_path):
    mgr, writer, store, repo, slot = await _setup_env(tmp_path, "test2.db")
    service = None
    started, release, calls = asyncio.Event(), asyncio.Event(), []
    try:
        async def solver(**kwargs):
            calls.append(kwargs["http_client"])
            started.set()
            await release.wait()
            return True, [{"name": "u", "value": "B-late", "domain": ".avito.ru"}]
        gateway = RequestGateway(RequestBudget(enforce=True, spacing_sec=7.5))
        service = ChallengeHandoffService(repo, store, http_pow_solver=solver,
            allow_browser=False, egress_gateway=gateway, egress_groups=EgressRegulatorConfig())
        req = ChallengeRequest("audit", challenge_token="tok", client_identity="user_b",
                               expected_generation=slot.generation, endpoint="direct")
        task = asyncio.create_task(service.handle_challenge(req))
        await started.wait()
        # Trigger graceful shutdown while challenge is in-flight
        await service.close()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(calls) == 1 and calls[0].is_closed
        # Late invocations after close must immediately fail closed without HTTP calls
        with pytest.raises(RuntimeError, match="closed"):
            await service.handle_challenge(req)
        assert len(calls) == 1
    finally:
        release.set()
        if service is not None:
            await service.close()
        await writer.stop()
        await mgr.drain_and_close()


@pytest.mark.asyncio
async def test_missing_generation_does_not_coalesce_and_does_not_reuse_reassigned_slot(tmp_path):
    mgr, writer, store, repo, slot = await _setup_env(tmp_path, "test3.db")
    service = None
    started, release, calls = asyncio.Event(), asyncio.Event(), []
    try:
        async def solver(**kwargs):
            calls.append(kwargs["http_client"])
            started.set()
            await release.wait()
            return True, [{"name": "u", "value": f"val-{len(calls)}", "domain": ".avito.ru"}]
        gateway = RequestGateway(RequestBudget(enforce=True, spacing_sec=7.5))
        service = ChallengeHandoffService(repo, store, http_pow_solver=solver,
            allow_browser=False, egress_gateway=gateway, egress_groups=EgressRegulatorConfig())
        # Request with expected_generation=None
        req1 = ChallengeRequest("audit", challenge_token="tok1", client_identity="user_b",
                                expected_generation=None, endpoint="direct")
        task1 = asyncio.create_task(service.handle_challenge(req1))
        await started.wait()
        # Singleflight tasks table must be empty: no coalescing key registered for None generation
        assert len(service._singleflight._tasks) == 0
        release.set()
        res1 = await task1
        assert res1.solved and len(calls) == 1
        # Now bump generation of slot
        await repo.bump_generation("audit")
        new_slot = await repo.get_slot("audit")
        assert new_slot.generation > slot.generation
        # Subsequent request with old slot reference cannot reuse recent refresh due to generation mismatch
        started.clear()
        release.clear()
        task2 = asyncio.create_task(service.handle_challenge(req1))
        await started.wait()
        # Solver was invoked again, not bypassed by _recent_refreshes
        assert len(calls) == 2
        release.set()
        await task2
    finally:
        release.set()
        if service is not None:
            await service.close()
        await writer.stop()
        await mgr.drain_and_close()


@pytest.mark.asyncio
async def test_recent_refreshes_user_a_success_does_not_grant_user_b_false_solved(tmp_path):
    mgr, writer, store, repo, slot = await _setup_env(tmp_path, "test4.db")
    service = None
    calls = []
    try:
        async def solver(**kwargs):
            calls.append(kwargs.get("endpoint"))
            return True, [{"name": "u", "value": f"v-{len(calls)}", "domain": ".avito.ru"}]
        gateway = RequestGateway(RequestBudget(enforce=True, spacing_sec=7.5))
        service = ChallengeHandoffService(repo, store, http_pow_solver=solver,
            allow_browser=False, egress_gateway=gateway, egress_groups=EgressRegulatorConfig())
        # First: solve challenge for user_a
        req_a = ChallengeRequest("audit", challenge_token="tok-a", client_identity="user_a",
                                 expected_generation=slot.generation, endpoint="direct")
        outcome_a = await service.handle_challenge(req_a)
        assert outcome_a.solved and len(calls) == 1
        # Now: request for user_b on the same slot within 15 seconds
        cur_slot = await repo.get_slot("audit")
        req_b = ChallengeRequest("audit", challenge_token="tok-b", client_identity="user_b",
                                 expected_generation=cur_slot.generation, endpoint="direct")
        outcome_b = await service.handle_challenge(req_b)
        # user_b must NOT get a false SOLVED from user_a's recent refresh!
        # It must invoke its own solver
        assert outcome_b.solved and len(calls) == 2
        assert "Session refreshed recently by concurrent worker" not in outcome_b.reason
    finally:
        if service is not None:
            await service.close()
        await writer.stop()
        await mgr.drain_and_close()
