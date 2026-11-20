"""Periodic owner alert decisions for observable public-runtime failures."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger as traffic_logger

from hunter.system.doctor import OwnerAlertLimiter, RuntimeHealthSignals, evaluate_runtime_health
from hunter.system.runtime_health import read_runtime_health

logger = logging.getLogger(__name__)


class RuntimeMonitor:
    def __init__(
        self,
        read_signals: Callable[[], Awaitable[RuntimeHealthSignals]],
        send_alert: Callable[[str], Awaitable[bool]] | None,
        *,
        interval_sec: float = 30.0,
        limiter: OwnerAlertLimiter | None = None,
        read_traffic: Callable[[], dict[str, Any] | None] | None = None,
        reconcile_assignments: Callable[[], Awaitable[list[str]]] | None = None,
    ) -> None:
        if interval_sec <= 0:
            raise ValueError("Monitor interval must be positive")
        self.read_signals = read_signals
        self.send_alert = send_alert
        self.interval_sec = interval_sec
        self.limiter = limiter or OwnerAlertLimiter()
        self.read_traffic = read_traffic
        self.reconcile_assignments = reconcile_assignments

    async def check_once(self) -> list[str]:
        if self.reconcile_assignments is not None:
            try:
                await self.reconcile_assignments()
            except Exception as exc:
                logger.warning("Assignment recovery check failed: %s", exc)
        if self.read_traffic is not None:
            summary = self.read_traffic()
            if summary is not None:
                traffic_logger.info("Poll response body traffic: {}", summary)
        check = evaluate_runtime_health(await self.read_signals())
        reasons = [r for r in check.details["reason_codes"] if not r.endswith("_unknown")]
        if not reasons:
            return []
        logger.warning("Public runtime degraded: %s", ",".join(reasons))
        if self.send_alert is not None:

            async def send(message: str) -> None:
                if await self.send_alert(message) is False:
                    raise RuntimeError("Owner alert delivery failed")

            key = ",".join(sorted(reasons))
            await self.limiter.send_once(key, f"Public runtime degraded: {key}", send)
        return reasons

    async def run(self) -> None:
        while True:
            try:
                await self.check_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Runtime health monitoring or alert delivery failed")
            await asyncio.sleep(self.interval_sec)


def start_runtime_monitor(bootstrap: Any) -> asyncio.Task:
    def read_traffic() -> dict[str, Any] | None:
        runtime = bootstrap.public_poll_runtime
        return runtime.metrics.response_body_summary() if runtime is not None else None

    async def reconcile_fn() -> list[str]:
        if bootstrap.public_poll_runtime is not None:
            from hunter.system.assignment_recovery import reconcile_runtime_assignments
            return await reconcile_runtime_assignments(bootstrap.public_poll_runtime)
        return []

    monitor = RuntimeMonitor(
        lambda: read_runtime_health(bootstrap.db),
        bootstrap.owner_alert_sender,
        read_traffic=read_traffic,
        reconcile_assignments=reconcile_fn,
    )
    return asyncio.create_task(monitor.run(), name="runtime_health_monitor")
