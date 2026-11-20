from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from hunter.core.config import Settings, get_settings
from hunter.core.db import Database
from hunter.core.models import now_utc
from hunter.system.doctor_parts.run_diagnostics_01 import (
    check_ai_credentials,
    check_backup_retention,
    check_circuit_breaker,
    check_database_integrity,
    check_disk_space,
)
from hunter.system.doctor_parts.run_diagnostics_03 import (
    check_process_memory,
    check_products_health,
    check_request_budget,
)

OUTBOX_AGE_WARN_SECONDS = 300  # CALIBRATE: pending delivery warning threshold.
WAL_WARN_BYTES = 256 * 1024 * 1024  # CALIBRATE: WAL size warning threshold.
RSS_WARN_BYTES = 1024 * 1024 * 1024  # CALIBRATE: RSS warning threshold.


@dataclass
class DiagnosticCheck:
    name: str
    status: Literal["ok", "warn", "fail"]
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class DoctorReport:
    timestamp: str
    is_healthy: bool
    checks: list[DiagnosticCheck]

    @property
    def health_state(self) -> Literal["healthy", "degraded", "unhealthy"]:
        if any(check.status == "fail" for check in self.checks):
            return "unhealthy"
        if any(check.status == "warn" for check in self.checks):
            return "degraded"
        return "healthy"

    def format_text(self) -> str:
        lines = [f"=== Hunter Doctor System Diagnostics ({self.timestamp}) ==="]
        for c in self.checks:
            badge = "[PASS]" if c.status == "ok" else ("[WARN]" if c.status == "warn" else "[FAIL]")
            lines.append(f"{badge:6} {c.name}: {c.message}")
            if c.details:
                for k, v in c.details.items():
                    lines.append(f"         • {k}: {v}")
        status_str = self.health_state.upper()
        lines.append(f"Overall Status: {status_str}")
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class RuntimeHealthSignals:
    relay_status: str = "unknown"
    relay_http_status: int | None = None
    pending_outbox_count: int = 0
    outbox_limit: int = 1000
    oldest_pending_age_sec: float = 0.0
    missed_windows: int | None = None
    quarantined_slots: int = 0
    wal_bytes: int = 0
    rss_bytes: int | None = None


def evaluate_runtime_health(signals: RuntimeHealthSignals) -> DiagnosticCheck:
    """Build a PII-free runtime health check and stable reason codes."""
    reasons: list[str] = []
    if signals.relay_http_status in {401, 403} or signals.relay_status == "down":
        reasons.append("relay_unavailable")
    elif signals.relay_status != "healthy":
        reasons.append("relay_unknown")
    if signals.pending_outbox_count >= signals.outbox_limit:
        reasons.append("outbox_overflow")
    if signals.oldest_pending_age_sec >= OUTBOX_AGE_WARN_SECONDS:
        reasons.append("outbox_oldest_age")
    if signals.missed_windows is None:
        reasons.append("poll_window_status_unknown")
    elif signals.missed_windows:
        reasons.append("missed_poll_windows")
    if signals.quarantined_slots:
        reasons.append("quarantined_slots")
    if signals.wal_bytes >= WAL_WARN_BYTES:
        reasons.append("wal_size_high")
    if signals.rss_bytes is None:
        reasons.append("rss_unknown")
    elif signals.rss_bytes >= RSS_WARN_BYTES:
        reasons.append("rss_high")
    status: Literal["ok", "warn", "fail"] = "warn" if reasons else "ok"
    return DiagnosticCheck(
        "public_runtime",
        status,
        "runtime degraded" if reasons else "runtime healthy",
        {"reason_codes": reasons},
    )


class OwnerAlertLimiter:
    """Deduplicates owner alerts by stable key and minimum interval."""

    def __init__(self, interval_sec: float = 600, clock: Callable[[], float] = time.monotonic):
        self.interval_sec = max(0.0, interval_sec)
        self.clock = clock
        self._last_sent: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def send_once(
        self,
        key: str,
        message: str,
        sender: Callable[[str], Awaitable[Any]],
    ) -> bool:
        async with self._lock:
            now = self.clock()
            last = self._last_sent.get(key)
            if last is not None and now - last < self.interval_sec:
                return False
            await sender(message)
            self._last_sent[key] = now
            return True


_owner_alert_limiter = OwnerAlertLimiter()


async def _append_runtime_health(
    checks: list[DiagnosticCheck],
    signals: RuntimeHealthSignals,
    sender: Callable[[str], Awaitable[Any]] | None,
    limiter: OwnerAlertLimiter | None,
) -> bool:
    check = evaluate_runtime_health(signals)
    checks.append(check)
    if check.status == "ok":
        return True
    if sender is not None:
        reasons = ",".join(check.details["reason_codes"])
        gate = limiter or _owner_alert_limiter
        await gate.send_once(
            "public_runtime_degraded", f"Public runtime degraded: {reasons}", sender
        )
    return False


async def _runtime_signals(
    checks: list[DiagnosticCheck], settings: Settings, db: Database | None,
    signals: RuntimeHealthSignals | None,
) -> tuple[RuntimeHealthSignals | None, bool]:
    if signals is not None or not settings.public_v2.enabled:
        return signals, True
    if db is None:
        checks.append(DiagnosticCheck(
            "public_runtime", "fail", "runtime storage unavailable",
            {"reason_codes": ["runtime_storage_unavailable"]},
        ))
        return None, False
    from hunter.system.runtime_health import read_runtime_health

    return await read_runtime_health(db), True


async def run_diagnostics(
    db: Database | None = None,
    settings: Settings | None = None,
    backups_dir: Path | None = None,
    runtime_health: RuntimeHealthSignals | None = None,
    owner_alert_sender: Callable[[str], Awaitable[Any]] | None = None,
    alert_limiter: OwnerAlertLimiter | None = None,
) -> DoctorReport:
    """Runs a full suite of self-diagnostic health checks on local environment."""
    settings = settings or get_settings()
    checks: list[DiagnosticCheck] = []
    overall_healthy = True

    db_check, db_ok = await check_database_integrity(db, settings)
    if not db_ok:
        overall_healthy = False
    checks.append(db_check)

    disk_check, disk_ok = check_disk_space(settings)
    if not disk_ok:
        overall_healthy = False
    checks.append(disk_check)

    checks.append(check_backup_retention(settings, backups_dir))

    checks.append(check_ai_credentials(settings))

    circuit_check, circuit_ok = await check_circuit_breaker(db, settings)
    if not circuit_ok:
        overall_healthy = False
    if circuit_check:
        checks.append(circuit_check)

    budget_check = await check_request_budget(db, settings)
    if budget_check:
        checks.append(budget_check)

    checks.append(check_process_memory())

    checks.append(check_products_health(settings))

    runtime_health, available = await _runtime_signals(checks, settings, db, runtime_health)
    overall_healthy = overall_healthy and available
    if runtime_health is not None:
        if not await _append_runtime_health(
            checks, runtime_health, owner_alert_sender, alert_limiter
        ):
            overall_healthy = False

    return DoctorReport(
        timestamp=now_utc().isoformat(),
        is_healthy=overall_healthy,
        checks=checks,
    )
