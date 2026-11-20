"""Accounting of physical HTTP attempts, valid catalog updates, gaps, and downtime (H-04)."""
from __future__ import annotations

import math
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Callable

ALLOWED_PURPOSES = {"poll", "warmup", "retry", "redirect", "challenge", "recovery"}


@dataclass(frozen=True, slots=True)
class QueryGapStats:
    mean_gap_sec: float | None
    p50_gap_sec: float | None
    p95_gap_sec: float | None
    downtime_sec: float
    tail_window_sec: float | None


class ValidMetricsTracker:
    """Tracks physical HTTP attempts, valid catalog updates, gaps, and downtime windows."""

    def __init__(
        self, clock: Callable[[], float] | None = None, *,
        max_gap_samples: int = 1000, attach_listener: bool = False,
    ):
        import time
        self.clock = clock or time.monotonic
        self.max_gap_samples = max_gap_samples

        self.physical_http_counts: dict[str, int] = defaultdict(int)
        self.scheduled_polls_done: int = 0
        self.scheduled_polls_skipped: int = 0

        self.last_valid_timestamp: dict[str, float] = {}
        self.query_gaps: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=self.max_gap_samples))
        self.total_valid_updates: int = 0

        self.quarantine_total_seconds: float = 0.0
        self.html_rediscovery_count: int = 0
        self._listener_attached = False
        if attach_listener:
            self.attach_listener()

    def handle_attempt_event(self, event: dict[str, Any]) -> None:
        if event.get("event") == "http_attempt_start":
            self.record_physical_http(event.get("purpose", "poll"))

    def attach_listener(self) -> None:
        if not self._listener_attached:
            from hunter.transport.http_attempts import register_attempt_listener
            register_attempt_listener(self.handle_attempt_event)
            self._listener_attached = True

    def detach_listener(self) -> None:
        if self._listener_attached:
            from hunter.transport.http_attempts import unregister_attempt_listener
            unregister_attempt_listener(self.handle_attempt_event)
            self._listener_attached = False

    def record_physical_http(self, purpose: str = "poll") -> None:
        """Records a physical wire HTTP request by low-cardinality purpose."""
        p = purpose if purpose in ALLOWED_PURPOSES else "poll"
        self.physical_http_counts[p] += 1

    def record_scheduled_poll(self, *, executed: bool) -> None:
        if executed:
            self.scheduled_polls_done += 1
        else:
            self.scheduled_polls_skipped += 1

    def record_quarantine_duration(self, seconds: float | None) -> None:
        if seconds is not None and seconds > 0 and math.isfinite(seconds):
            self.quarantine_total_seconds += seconds

    def record_html_rediscovery(self) -> None:
        self.html_rediscovery_count += 1

    def record_valid_update(self, query_id: str, items_count: int = 0, now: float | None = None) -> bool:
        """Records a proven valid catalog update (parsed catalog + state update).

        Rejects empty or failing results (e.g. 429 or challenge shells).
        """
        if not query_id:
            return False
        ts = self.clock() if now is None else now
        prev_ts = self.last_valid_timestamp.get(query_id)
        if prev_ts is not None:
            gap = max(0.0, ts - prev_ts)
            self.query_gaps[query_id].append(gap)
        self.last_valid_timestamp[query_id] = ts
        self.total_valid_updates += 1
        return True

    def query_gap_summary(
        self, query_id: str, now: float | None = None, expected_interval_sec: float = 300.0
    ) -> QueryGapStats:
        """Calculates mean, p50, p95 gaps and downtime, incorporating unfinished tail window."""
        ts = self.clock() if now is None else now
        gaps = list(self.query_gaps.get(query_id, []))
        last_ts = self.last_valid_timestamp.get(query_id)

        current_tail = (ts - last_ts) if last_ts is not None else None
        effective_gaps = list(gaps)
        if current_tail is not None and current_tail > 0:
            effective_gaps.append(current_tail)

        if not effective_gaps:
            return QueryGapStats(
                mean_gap_sec=None, p50_gap_sec=None, p95_gap_sec=None,
                downtime_sec=current_tail if current_tail is not None else 0.0,
                tail_window_sec=current_tail,
            )

        sorted_gaps = sorted(effective_gaps)
        mean_gap = sum(sorted_gaps) / len(sorted_gaps)
        p50 = sorted_gaps[int(len(sorted_gaps) * 0.5)]
        idx95 = min(len(sorted_gaps) - 1, int(len(sorted_gaps) * 0.95))
        p95 = sorted_gaps[idx95]

        # Downtime accumulates all gap durations exceeding expected_interval
        downtime = sum(max(0.0, g - expected_interval_sec) for g in effective_gaps)

        return QueryGapStats(
            mean_gap_sec=round(mean_gap, 2),
            p50_gap_sec=round(p50, 2),
            p95_gap_sec=round(p95, 2),
            downtime_sec=round(downtime, 2),
            tail_window_sec=round(current_tail, 2) if current_tail is not None else None,
        )

    def total_physical_http(self) -> int:
        return sum(self.physical_http_counts.values())

    def attempts_per_valid_update(self) -> float | None:
        if self.total_valid_updates == 0:
            return None
        return round(self.total_physical_http() / self.total_valid_updates, 2)

    def summary(self, now: float | None = None) -> dict[str, Any]:
        return {
            "total_physical_http": self.total_physical_http(),
            "http_by_purpose": dict(self.physical_http_counts),
            "total_valid_updates": self.total_valid_updates,
            "attempts_per_valid_update": self.attempts_per_valid_update(),
            "scheduled_polls_done": self.scheduled_polls_done,
            "scheduled_polls_skipped": self.scheduled_polls_skipped,
            "quarantine_minutes": round(self.quarantine_total_seconds / 60.0, 2),
            "html_rediscovery_count": self.html_rediscovery_count,
        }
