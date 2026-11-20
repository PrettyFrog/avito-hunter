from unittest.mock import AsyncMock

import pytest

from hunter.system.doctor import RuntimeHealthSignals
from hunter.system.runtime_monitor import RuntimeMonitor


@pytest.mark.asyncio
async def test_monitor_alerts_actual_fault_once_and_ignores_unknown_only():
    read = AsyncMock(
        side_effect=[
            RuntimeHealthSignals(),
            RuntimeHealthSignals(quarantined_slots=1),
            RuntimeHealthSignals(quarantined_slots=1),
        ]
    )
    send = AsyncMock(return_value=True)
    monitor = RuntimeMonitor(read, send)
    await monitor.check_once()
    send.assert_not_awaited()
    await monitor.check_once()
    await monitor.check_once()
    send.assert_awaited_once()
    assert "quarantined_slots" in send.await_args.args[0]
    assert "relay_unknown" not in send.await_args.args[0]


@pytest.mark.asyncio
async def test_monitor_retries_unsuccessful_alert():
    read = AsyncMock(return_value=RuntimeHealthSignals(quarantined_slots=1))
    send = AsyncMock(side_effect=[False, True])
    monitor = RuntimeMonitor(read, send)
    with pytest.raises(RuntimeError):
        await monitor.check_once()
    await monitor.check_once()
    assert send.await_count == 2


@pytest.mark.asyncio
async def test_monitor_logs_aggregate_traffic_without_identifiers():
    from loguru import logger

    read = AsyncMock(return_value=RuntimeHealthSignals())
    summary = {
        "scope": "recent_in_memory_samples",
        "measured_samples": 2,
        "samples": 2,
        "body_bytes": 400,
        "mean_body_bytes": 200.0,
    }
    monitor = RuntimeMonitor(read, None, read_traffic=lambda: summary)
    messages = []
    sink = logger.add(lambda message: messages.append(str(message)), level="INFO")
    try:
        await monitor.check_once()
    finally:
        logger.remove(sink)
    assert "Poll response body traffic" in messages[0]
    assert "400" in messages[0]
    assert "slot" not in messages[0]
