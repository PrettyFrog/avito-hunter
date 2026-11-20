from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from hunter.bootstrap import Bootstrap
from hunter.core.config import Settings
from hunter.core.db import Database
from hunter.domain.slots import SlotRole, SlotSignal
from hunter.storage.subscriptions import SubscriptionRecord
from hunter.transport.http_fetch import SlotHttpPool
from hunter.transport.session_store import SessionStore


async def _prepared_poll_bootstrap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_transport: httpx.AsyncBaseTransport,
    user_id: int,
) -> tuple[Bootstrap, SubscriptionRecord]:
    monkeypatch.setenv("HUNTER_DATA_DIR", str(tmp_path))
    settings = Settings(env="test")
    settings.public_v2.enabled = True
    bootstrap = Bootstrap(
        settings,
        db=Database(tmp_path / "poll-fault.db"),
        senders={"telegram": AsyncMock()},
    )
    await bootstrap.startup()
    await bootstrap.slot_repo.register_slot("source", "http://source:8080", role=SlotRole.ACTIVE)
    await bootstrap.slot_repo.register_slot("reserve", "http://reserve:8080", role=SlotRole.RESERVE)

    async def empty(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="", request=request)

    bootstrap.http_pool.get_fetcher("source", "http://source:8080", transport=source_transport)
    bootstrap.http_pool.get_fetcher(
        "reserve", "http://reserve:8080", transport=httpx.MockTransport(empty)
    )
    await bootstrap.writer.submit(
        lambda conn: conn.execute(
            "INSERT INTO users (id, role, created_at) VALUES (?, 'user', datetime('now'))",
            (user_id,),
        )
    )
    bootstrap.subscription_service.auto_assign = False
    subscription = await bootstrap.subscription_service.add_subscription(
        user_id, f"https://avito.ru/moskva?q={user_id}", 25000
    )
    await bootstrap.slot_repo.assign_query("source", subscription.query_id)
    await bootstrap.public_poll_runtime.refresh(start=False)
    return bootstrap, subscription


@pytest.mark.asyncio
async def test_fault_403_quarantine_routes_live_poll_to_reserve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def blocked(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, request=request)

    bootstrap, subscription = await _prepared_poll_bootstrap(
        tmp_path, monkeypatch, httpx.MockTransport(blocked), 7710
    )
    try:
        worker = bootstrap.public_poll_runtime.workers["source"]
        await worker.start()

        async def wait_for_transfer() -> None:
            while True:
                reserve = await bootstrap.slot_repo.get_slot("reserve")
                if subscription.query_id in reserve.capacity.assigned_queries:
                    return
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait_for_transfer(), timeout=2)
        assert worker._task is not None
        await asyncio.wait_for(worker._task, timeout=2)
        source = await bootstrap.slot_repo.get_slot("source")
        reserve = await bootstrap.slot_repo.get_slot("reserve")
        assert source.health.value == "QUARANTINED"
        assert source.quarantined_until is not None
        assert reserve.capacity.assigned_queries == (subscription.query_id,)
        assert subscription.query_id not in source.capacity.assigned_queries
    finally:
        await bootstrap.shutdown()


@pytest.mark.asyncio
async def test_fault_stale_403_response_cannot_quarantine_reassigned_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request_started = asyncio.Event()
    release_response = asyncio.Event()

    async def delayed_block(request: httpx.Request) -> httpx.Response:
        request_started.set()
        await release_response.wait()
        return httpx.Response(403, request=request)

    bootstrap, subscription = await _prepared_poll_bootstrap(
        tmp_path, monkeypatch, httpx.MockTransport(delayed_block), 7712
    )
    try:
        assignment = bootstrap.public_poll_runtime.assignments[("source", subscription.query_id)]
        stale_generation = await bootstrap.supervisor.handle_slot_signal(
            "source",
            SlotSignal.HTTP_403,
            query_id=subscription.query_id,
            assignment_generation=assignment.generation + 1,
        )
        assert stale_generation.status == "stale_signal"
        assert (await bootstrap.slot_repo.get_slot("source")).health.value == "READY"
        poll_task = asyncio.create_task(bootstrap.public_poll_runtime.poll(assignment, sequence=1))
        await asyncio.wait_for(request_started.wait(), timeout=2)
        await bootstrap.slot_repo.transfer_to_spare("source", "reserve", subscription.query_id)
        release_response.set()
        await asyncio.wait_for(poll_task, timeout=2)

        source = await bootstrap.slot_repo.get_slot("source")
        reserve = await bootstrap.slot_repo.get_slot("reserve")
        assert source.health.value == "READY"
        assert subscription.query_id not in source.capacity.assigned_queries
        assert reserve.capacity.assigned_queries == (subscription.query_id,)
    finally:
        release_response.set()
        await bootstrap.shutdown()


@pytest.mark.asyncio
async def test_fault_disabled_slot_stops_fetch_and_refreshes_after_enable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, text="", request=request)

    bootstrap, subscription = await _prepared_poll_bootstrap(
        tmp_path, monkeypatch, httpx.MockTransport(respond), 7714
    )
    try:
        runtime = bootstrap.public_poll_runtime
        old = runtime.assignments[("source", subscription.query_id)]
        await bootstrap.slot_repo.disable_slot("source")
        assert await runtime.poll(old, sequence=1) is None
        assert calls == 0

        # A process restarted while the slot is disabled still loads a parked assignment.
        await runtime.refresh(start=False)
        parked = runtime.assignments[("source", subscription.query_id)]
        assert await runtime.poll(parked, sequence=2) is None
        assert calls == 0

        await bootstrap.slot_repo.enable_slot("source")
        await runtime.poll(parked, sequence=3)
        assert calls == 1
    finally:
        await bootstrap.shutdown()


@pytest.mark.asyncio
async def test_fault_timeout_routes_active_query_to_reserve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated timeout", request=request)

    bootstrap, subscription = await _prepared_poll_bootstrap(
        tmp_path, monkeypatch, httpx.MockTransport(timeout), 7711
    )
    try:
        assignment = bootstrap.public_poll_runtime.assignments[("source", subscription.query_id)]
        await bootstrap.public_poll_runtime.poll(assignment, sequence=1)
        source = await bootstrap.slot_repo.get_slot("source")
        reserve = await bootstrap.slot_repo.get_slot("reserve")
        assert source.health.value == "SUSPECT"
        assert reserve.capacity.assigned_queries == (subscription.query_id,)
    finally:
        await bootstrap.shutdown()


@pytest.mark.parametrize(
    ("status", "body", "health"),
    [
        (429, "", "SUSPECT"),
        (200, "Войти или зарегистрироваться", "AUTH_REQUIRED"),
        (200, "firewall/captcha", "CHALLENGE"),
    ],
)
@pytest.mark.asyncio
async def test_fault_classified_response_routes_active_query_to_reserve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    body: str,
    health: str,
) -> None:
    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=body, request=request)

    bootstrap, subscription = await _prepared_poll_bootstrap(
        tmp_path,
        monkeypatch,
        httpx.MockTransport(respond),
        7713 + status + len(body),
    )
    try:
        assignment = bootstrap.public_poll_runtime.assignments[("source", subscription.query_id)]
        await bootstrap.public_poll_runtime.poll(assignment, sequence=1)
        source = await bootstrap.slot_repo.get_slot("source")
        reserve = await bootstrap.slot_repo.get_slot("reserve")
        assert source.health.value == health
        assert reserve.capacity.assigned_queries == (subscription.query_id,)
    finally:
        await bootstrap.shutdown()


@pytest.mark.asyncio
async def test_failover_starts_target_slot_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HUNTER_DATA_DIR", str(tmp_path))
    settings = Settings(env="test")
    settings.public_v2.enabled = True
    db = Database(tmp_path / "failover-runtime.db")
    bootstrap = Bootstrap(settings, db=db, senders={"telegram": AsyncMock()})
    await bootstrap.startup()
    try:
        await bootstrap.slot_repo.register_slot(
            "source", "http://source:8080", role=SlotRole.ACTIVE
        )
        await bootstrap.slot_repo.register_slot(
            "reserve", "http://reserve:8080", role=SlotRole.RESERVE
        )

        async def respond(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="", request=request)

        for slot_id, endpoint in (
            ("source", "http://source:8080"),
            ("reserve", "http://reserve:8080"),
        ):
            bootstrap.http_pool.get_fetcher(
                slot_id, endpoint, transport=httpx.MockTransport(respond)
            )
        await bootstrap.writer.submit(
            lambda conn: conn.execute(
                "INSERT INTO users (id, role, created_at) VALUES (7701, 'user', datetime('now'))"
            )
        )
        subscription = await bootstrap.subscription_service.add_subscription(
            7701, "https://avito.ru/moskva?q=phone", 50000
        )
        assert "source" in bootstrap.workers

        outcome = await bootstrap.supervisor.handle_slot_signal("source", SlotSignal.HTTP_403)

        assert outcome.status == "failover_complete"
        assert outcome.transferred_queries == [subscription.query_id]
        assert "source" not in bootstrap.workers
        target = bootstrap.workers["reserve"]
        assert target.assigned_queries == [subscription.query_id]
        assert target._task is not None and not target._task.done()
    finally:
        await bootstrap.shutdown()


@pytest.mark.asyncio
async def test_restart_reconciles_transfer_committed_before_worker_activation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HUNTER_DATA_DIR", str(tmp_path))
    settings = Settings(env="test")
    settings.public_v2.enabled = True
    db_path = tmp_path / "failover-restart.db"
    bootstrap = Bootstrap(settings, db=Database(db_path), senders={"telegram": AsyncMock()})
    await bootstrap.startup()

    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="", request=request)

    for slot_id in ("source", "reserve"):
        endpoint = f"http://{slot_id}:8080"
        await bootstrap.slot_repo.register_slot(
            slot_id, endpoint, role=SlotRole.ACTIVE if slot_id == "source" else SlotRole.RESERVE
        )
        bootstrap.http_pool.get_fetcher(slot_id, endpoint, transport=httpx.MockTransport(respond))
    await bootstrap.writer.submit(
        lambda conn: conn.execute(
            "INSERT INTO users (id, role, created_at) VALUES (7702, 'user', datetime('now'))"
        )
    )
    subscription = await bootstrap.subscription_service.add_subscription(
        7702, "https://avito.ru/moskva?q=tablet", 40000
    )
    await bootstrap.public_poll_runtime.stop()

    async def crash_after_transfer(_query_id: str) -> None:
        raise RuntimeError("simulated process loss after assignment commit")

    bootstrap.supervisor.on_assignment_changed = crash_after_transfer
    with pytest.raises(RuntimeError, match="after assignment commit"):
        await bootstrap.supervisor.handle_slot_signal("source", SlotSignal.HTTP_403)
    assert (await bootstrap.slot_repo.get_slot("reserve")).capacity.assigned_queries == (
        subscription.query_id,
    )
    await bootstrap.shutdown()

    restarted_pool = SlotHttpPool(session_store=SessionStore(base_dir=tmp_path / "sessions"))
    restarted_pool.get_fetcher(
        "reserve", "http://reserve:8080", transport=httpx.MockTransport(respond)
    )
    restarted = Bootstrap(
        settings, db=Database(db_path), senders={"telegram": AsyncMock()}, http_pool=restarted_pool
    )
    await restarted.startup()
    try:
        worker = restarted.workers["reserve"]
        assert worker.assigned_queries == [subscription.query_id]
        assert worker._task is not None and not worker._task.done()
    finally:
        await restarted.shutdown()
