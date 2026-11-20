from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock
import pytest

from hunter.application.public_poll_runtime import PublicPollRuntime, QueryAssignment
from hunter.domain.poll_policy import PollPolicy
from hunter.system.assignment_recovery import reconcile_runtime_assignments


@pytest.fixture
def fake_runtime():
    pool = MagicMock()
    fetcher = MagicMock()
    fetcher.load_cookies_from_store = MagicMock(return_value=None)
    pool.get_fetcher = MagicMock(return_value=fetcher)
    conn_mgr = MagicMock()
    runtime = PublicPollRuntime(
        conn_mgr=conn_mgr,
        http_pool=pool,
        ingest=AsyncMock(),
        policy=PollPolicy(1, 1),
    )
    return runtime


@pytest.mark.asyncio
async def test_reconcile_recovers_missing_worker_without_resetting_healthy(fake_runtime):
    """T-957: Terminated worker is recovered while healthy workers retain schedule."""
    asg_1 = QueryAssignment("slot_1", "direct", "q_1", "https://avito.ru/q1", 1, 0)
    asg_2 = QueryAssignment("slot_2", "direct", "q_2", "https://avito.ru/q2", 1, 0)

    # Initialize slot_1 as running healthy worker
    await fake_runtime._replace_slot("slot_1", [asg_1], start=False)
    healthy_worker = fake_runtime.workers["slot_1"]
    healthy_worker._running = True
    healthy_worker._task = asyncio.create_task(asyncio.sleep(10))

    from unittest.mock import patch
    with patch("hunter.system.assignment_recovery.load_active_assignments", AsyncMock(return_value=[asg_1, asg_2])):
        recovered = await reconcile_runtime_assignments(fake_runtime)

    assert recovered == ["slot_2"]
    # Healthy slot_1 worker was NOT replaced or reset
    assert fake_runtime.workers["slot_1"] is healthy_worker
    # Missing slot_2 worker was recovered
    assert "slot_2" in fake_runtime.workers

    healthy_worker._task.cancel()
    await asyncio.gather(healthy_worker._task, return_exceptions=True)


@pytest.mark.asyncio
async def test_reconcile_does_not_run_after_shutdown(fake_runtime):
    """T-957: Workers must not be launched after runtime shutdown."""
    asg = QueryAssignment("slot_1", "direct", "q_1", "https://avito.ru/q1", 1, 0)
    await fake_runtime.stop()

    from unittest.mock import patch
    with patch("hunter.system.assignment_recovery.load_active_assignments", AsyncMock(return_value=[asg])):
        recovered = await reconcile_runtime_assignments(fake_runtime)

    assert recovered == []
    assert "slot_1" not in fake_runtime.workers


@pytest.mark.asyncio
async def test_runtime_monitor_runs_reconcile():
    """T-957: RuntimeMonitor executes reconcile_assignments during periodic check."""
    reconciled = False

    async def fake_reconcile():
        nonlocal reconciled
        reconciled = True
        return ["slot_1"]

    from hunter.system.runtime_monitor import RuntimeMonitor
    from hunter.system.doctor import RuntimeHealthSignals

    async def fake_signals():
        return RuntimeHealthSignals(0, 0, 0, 0, 0, 0, 0)

    monitor = RuntimeMonitor(
        fake_signals,
        None,
        reconcile_assignments=fake_reconcile,
    )
    await monitor.check_once()
    assert reconciled is True


@pytest.mark.asyncio
async def test_reconcile_updates_healthy_worker_queries_in_place(fake_runtime):
    """T-957: Healthy worker gets new durable queries without restarting task."""
    asg_old = QueryAssignment("slot_1", "direct", "q_old", "https://avito.ru/old", 1, 0)
    asg_new = QueryAssignment("slot_1", "direct", "q_new", "https://avito.ru/new", 1, 1)

    await fake_runtime._replace_slot("slot_1", [asg_old], start=False)
    healthy_worker = fake_runtime.workers["slot_1"]
    healthy_worker._running = True
    healthy_worker._task = asyncio.create_task(asyncio.sleep(10))

    from unittest.mock import patch
    with patch("hunter.system.assignment_recovery.load_active_assignments", AsyncMock(return_value=[asg_old, asg_new])):
        recovered = await reconcile_runtime_assignments(fake_runtime)

    assert recovered == ["slot_1"]
    assert fake_runtime.workers["slot_1"] is healthy_worker
    assert sorted(healthy_worker.assigned_queries) == ["q_new", "q_old"]

    healthy_worker._task.cancel()
    await asyncio.gather(healthy_worker._task, return_exceptions=True)


@pytest.mark.asyncio
async def test_reconcile_updates_metadata_and_retires_fetcher_on_endpoint_change(fake_runtime):
    """T-957: Endpoint/generation change in DB triggers fetcher retirement and updates metadata."""
    asg_initial = QueryAssignment("slot_1", "direct", "q_1", "https://avito.ru/q1", 1, 0, slot_generation=1)
    asg_updated = QueryAssignment("slot_1", "http://proxy.test:8080", "q_1", "https://avito.ru/q1", 1, 0, slot_generation=2)

    await fake_runtime._replace_slot("slot_1", [asg_initial], start=False)
    healthy_worker = fake_runtime.workers["slot_1"]
    healthy_worker._running = True
    healthy_worker._task = asyncio.create_task(asyncio.sleep(10))

    retired = []
    fake_runtime.http_pool.retire_fetcher = AsyncMock(side_effect=lambda sid: retired.append(sid))

    from unittest.mock import patch
    with patch("hunter.system.assignment_recovery.load_active_assignments", AsyncMock(return_value=[asg_updated])):
        recovered = await reconcile_runtime_assignments(fake_runtime)

    assert recovered == ["slot_1"]
    assert retired == ["slot_1"]
    assert fake_runtime.workers["slot_1"] is healthy_worker
    actual = fake_runtime.assignments[("slot_1", "q_1")]
    assert actual.endpoint == "http://proxy.test:8080"
    assert actual.slot_generation == 2

    healthy_worker._task.cancel()
    await asyncio.gather(healthy_worker._task, return_exceptions=True)
