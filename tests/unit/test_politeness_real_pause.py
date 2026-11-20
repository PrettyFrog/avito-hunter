import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hunter.collect.politeness import Politeness


@pytest.mark.asyncio
async def test_politeness_enforces_real_pause_from_completion():
    mock_db = MagicMock()
    mock_db.is_paused = AsyncMock(return_value=False)
    mock_db.get_runtime = AsyncMock(return_value=None)
    mock_db.today_requests = AsyncMock(return_value=10)
    mock_db.get_daily_cap = AsyncMock(return_value=1000)

    polite = Politeness(mock_db)

    settings = MagicMock()
    settings.request_budget.min_delay_seconds = 1.5
    settings.request_budget.max_delay_seconds = 1.5
    settings.request_budget.night_multiplier = 1.0
    settings.request_budget.night_hours = [1, 7]
    settings.request_budget.jitter = 0.0
    settings.collect.light_min_delay_seconds = 1.5
    settings.collect.light_max_delay_seconds = 1.5
    settings.collect.global_min_gap_seconds = 1.0

    # Request 1 finished 0.2 seconds ago
    polite._last_finish_time = time.monotonic() - 0.2
    polite._last_profile_finish["profile_0"] = polite._last_finish_time

    sleep_calls: list[float] = []

    async def _mock_sleep(sec: float):
        sleep_calls.append(sec)

    with (
        patch("hunter.collect.politeness.get_settings", return_value=settings),
        patch("asyncio.sleep", side_effect=_mock_sleep),
    ):
        await polite.before_request(is_light=True)

        assert len(sleep_calls) == 1
        # Target wait is 1.5s, elapsed is ~0.2s, so sleep should be ~1.3s
        assert 1.2 <= sleep_calls[0] <= 1.4


@pytest.mark.asyncio
async def test_request_gate_serializes_and_records_finish():
    mock_db = MagicMock()
    mock_db.is_paused = AsyncMock(return_value=False)
    mock_db.get_runtime = AsyncMock(return_value=None)
    mock_db.today_requests = AsyncMock(return_value=10)
    mock_db.get_daily_cap = AsyncMock(return_value=1000)

    polite = Politeness(mock_db)

    settings = MagicMock()
    settings.request_budget.min_delay_seconds = 0.5
    settings.request_budget.max_delay_seconds = 0.5
    settings.request_budget.night_multiplier = 1.0
    settings.request_budget.night_hours = [1, 7]
    settings.request_budget.jitter = 0.0
    settings.collect.light_min_delay_seconds = 0.5
    settings.collect.light_max_delay_seconds = 0.5
    settings.collect.global_min_gap_seconds = 0.5

    order: list[str] = []

    async def worker(worker_id: str, hold_duration: float):
        with patch("hunter.collect.politeness.get_settings", return_value=settings):
            async with polite.request_gate(is_light=True):
                order.append(f"start_{worker_id}")
                await asyncio.sleep(hold_duration)
                order.append(f"end_{worker_id}")

    # Launch worker 1 (holds for 0.05s) and worker 2 concurrently
    t1 = asyncio.create_task(worker("1", 0.05))
    await asyncio.sleep(0.01)
    t2 = asyncio.create_task(worker("2", 0.02))

    await asyncio.gather(t1, t2)

    # Worker 2 must NOT start before Worker 1 ends!
    assert order == ["start_1", "end_1", "start_2", "end_2"]
    assert polite._last_finish_time > 0


@pytest.mark.asyncio
async def test_browser_restarts_on_target_closed_error():
    from hunter.collect.browser import BrowserSession, is_target_closed_error

    browser = BrowserSession()
    mock_context = MagicMock()
    mock_context.is_closed = MagicMock(return_value=False)
    browser.context = mock_context
    browser.restart = AsyncMock()

    # Simulate evaluate_in_tab_fetch encountering TargetClosedError
    from playwright._impl._errors import TargetClosedError

    err = TargetClosedError(
        "BrowserContext.new_page: Target page, context or browser has been closed"
    )
    assert is_target_closed_error(err) is True

    mock_page = AsyncMock()
    mock_page.evaluate = AsyncMock(side_effect=err)
    mock_page.is_closed = MagicMock(return_value=False)

    with patch.object(browser, "get_or_create_light_page", AsyncMock(return_value=mock_page)):
        status, url, html, error_msg = await browser.evaluate_in_tab_fetch(
            "https://www.avito.ru/test"
        )
        assert status == 0
        assert "Target page, context or browser has been closed" in str(error_msg)
        browser.restart.assert_called_once()
