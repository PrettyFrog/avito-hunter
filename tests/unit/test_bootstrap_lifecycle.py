"""Unit tests for composition root, lifecycle, and supervised shutdown (S6 / T-489).

Covers:
- Public v2 flag default OFF in Settings.
- Startup protocol: migrate and recover before background tasks.
- Supervised shutdown protocol: stop -> drain -> close.
- Supervised isolation: ordinary slot failure is isolated, process stays alive.
- Fatal task returns non-zero exit code (1).
- 0 leaked tasks and clients on shutdown.
- Owner extension optional (public path functions without AI providers).
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from hunter.bootstrap import Bootstrap
from hunter.core.config import Settings
from hunter.domain.slots import SlotHealth, SlotRole, SlotSignal


@pytest.fixture
def bootstrap_env(tmp_path: Path):
    db_path = tmp_path / "test_boot.db"
    settings = Settings(
        data_dir=tmp_path / "data",
        db_path=db_path,
    )
    return settings, db_path


@pytest.mark.asyncio
async def test_bootstrap_public_v2_flag_default_off(bootstrap_env):
    settings, _ = bootstrap_env
    assert settings.public_v2.enabled is False

    bs = Bootstrap(settings)
    await bs.startup()

    assert bs.slot_repo is not None
    assert bs.delivery_repo is not None
    # With public_v2.enabled = False, dispatcher background task is not spawned
    assert bs.dispatcher._running is False
    assert len(bs._tasks) == 0

    await bs.shutdown()


@pytest.mark.asyncio
async def test_bootstrap_owns_one_egress_gateway_until_shutdown(bootstrap_env):
    settings, _ = bootstrap_env
    settings.public_v2.enabled = True
    from hunter.core.egress_config import EgressRegulatorConfig
    settings.egress_regulator = EgressRegulatorConfig(mode="enforce", interval_sec=7.5,
                                                      night_interval_sec=45)
    bs = Bootstrap(settings, senders={"telegram": AsyncMock()})
    try:
        await bs.startup()
        assert bs.egress_gateway is not None
        assert bs.http_pool.gateway is bs.egress_gateway
        assert bs.challenge_service.egress_gateway is bs.egress_gateway
        assert bs.egress_gateway.budget.enforce
        assert not bs.egress_gateway.budget.closed
    finally:
        await bs.shutdown()
    assert bs.egress_gateway.budget.closed


@pytest.mark.asyncio
async def test_egress_gateway_rejects_unmetered_browser_path(bootstrap_env):
    from hunter.core.egress_config import EgressRegulatorConfig
    settings, _ = bootstrap_env
    settings.public_v2.enabled = True
    settings.public_v2.browser_fetch = True
    settings.egress_regulator = EgressRegulatorConfig(mode="enforce", interval_sec=7.5)
    bs = Bootstrap(settings, senders={"telegram": AsyncMock()})
    try:
        with pytest.raises(RuntimeError, match="cannot be bypassed"):
            await bs.startup()
    finally:
        await bs.shutdown()


@pytest.mark.asyncio
async def test_bootstrap_startup_migrate_and_recover_before_tasks(bootstrap_env):
    settings, db_path = bootstrap_env
    settings.public_v2.enabled = True
    with sqlite3.connect(db_path) as conn:
        conn.executescript(Path("src/hunter/core/schema.sql").read_text())

    mock_db = AsyncMock()
    mock_db.path = db_path
    mock_db.migrate = AsyncMock()
    mock_db.clear_all_pauses = AsyncMock()
    mock_db.close = AsyncMock()

    bs = Bootstrap(settings, db=mock_db, senders={"telegram": AsyncMock()})
    try:
        await bs.startup()

        assert mock_db.migrate.call_count == 1
        assert mock_db.clear_all_pauses.call_count == 1
        assert bs.dispatcher._task is not None
        assert not bs.dispatcher._task.done()
    finally:
        await bs.shutdown()


@pytest.mark.asyncio
async def test_bootstrap_supervised_shutdown_stop_drain_close(bootstrap_env):
    settings, _ = bootstrap_env
    settings.public_v2.enabled = True

    bs = Bootstrap(settings, senders={"telegram": AsyncMock()})
    worker_mock = AsyncMock()
    worker_mock.stop = AsyncMock()
    bs.workers["act_1"] = worker_mock

    stop_event = asyncio.Event()

    async def trigger_stop():
        await asyncio.sleep(0.05)
        stop_event.set()

    asyncio.create_task(trigger_stop())
    exit_code = await bs.run_supervised(stop_event)

    assert exit_code == 0
    assert worker_mock.stop.call_count == 1
    assert bs._is_shutdown is True
    # Verify 0 leaked tasks
    assert len(bs._tasks) == 0


@pytest.mark.asyncio
async def test_bootstrap_ordinary_slot_failure_isolated(bootstrap_env):
    settings, _ = bootstrap_env
    settings.public_v2.enabled = True

    bs = Bootstrap(settings, senders={"telegram": AsyncMock()})
    await bs.startup()

    # Register active and reserve slots
    await bs.slot_repo.register_slot("act_01", "http://10.0.0.1:8080", role=SlotRole.ACTIVE)
    await bs.slot_repo.register_slot("res_01", "http://10.0.0.2:8080", role=SlotRole.RESERVE)

    # Ordinary failure: 403 on act_01
    outcome = await bs.supervisor.handle_slot_signal("act_01", SlotSignal.HTTP_403)
    assert outcome.success is True
    assert outcome.new_health == SlotHealth.QUARANTINED

    # Process remains alive, not crashed
    assert bs._fatal_error is None

    await bs.shutdown()


@pytest.mark.asyncio
async def test_bootstrap_fatal_task_returns_nonzero(bootstrap_env):
    settings, _ = bootstrap_env
    settings.public_v2.enabled = True

    bs = Bootstrap(settings, senders={"telegram": AsyncMock()})
    await bs.startup()

    # Inject a fatal failing background task
    async def fatal_loop():
        await asyncio.sleep(0.02)
        raise RuntimeError("Fatal database corruption")

    fatal_task = asyncio.create_task(fatal_loop(), name="fatal_task")
    bs._tasks.append(fatal_task)

    stop_event = asyncio.Event()
    exit_code = await bs.run_supervised(stop_event)

    assert exit_code == 1
    assert bs._fatal_error is not None
    assert isinstance(bs._fatal_error, RuntimeError)


@pytest.mark.asyncio
async def test_bootstrap_owner_extension_optional(bootstrap_env):
    settings, _ = bootstrap_env
    # Public v2 functions without AI / owner extension
    bs = Bootstrap(settings)
    await bs.startup()

    assert bs.challenge_service is not None
    assert bs.admission_ctrl is not None
    # No error raised even if AI providers are completely unconfigured
    await bs.shutdown()


@pytest.mark.asyncio
async def test_public_v2_rejects_missing_telegram_sender_before_startup(bootstrap_env):
    settings, _ = bootstrap_env
    settings.public_v2.enabled = True
    bs = Bootstrap(settings)

    with pytest.raises(RuntimeError, match="(?i)telegram.*sender"):
        await bs.startup()

    assert bs.dispatcher is None
    assert bs.writer is None


@pytest.mark.asyncio
async def test_ingest_observation_wakes_up_dispatcher():
    from unittest.mock import AsyncMock, MagicMock
    from hunter.bootstrap import Bootstrap

    bs = MagicMock(spec=Bootstrap)
    bs.writer = AsyncMock()
    mock_res = MagicMock(deliveries_created=2)
    bs.writer.submit.return_value = mock_res
    bs.dispatcher = MagicMock()

    res = await Bootstrap.ingest_observation(bs, batch=MagicMock(), request_sequence=1)
    assert res is mock_res
    bs.dispatcher.wake_up.assert_called_once()

    bs.dispatcher.wake_up.reset_mock()
    mock_res.deliveries_created = 0
    await Bootstrap.ingest_observation(bs, batch=MagicMock(), request_sequence=1)
    bs.dispatcher.wake_up.assert_not_called()
