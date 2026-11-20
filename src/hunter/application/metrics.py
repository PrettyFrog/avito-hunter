"""Latency provenance and deterministic health metrics (S7 / T-493)."""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from uuid import uuid4

from hunter.application.valid_metrics import QueryGapStats, ValidMetricsTracker

LATENCY_BUCKET_LABELS = ("<10s", "10-30s", "30-60s", "1-5m", "5-15m", ">15m")
METRIC_SAMPLE_LIMIT = 1000


def _nonnegative_finite(value: str) -> float | None:
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) and number >= 0 else None


def classify_cache_freshness(
    headers: dict[str, str], now: datetime | None = None
) -> tuple[str, float | None, tuple[str, ...]]:
    """Return cache state and safe header-presence metadata without retaining values."""
    normalized = {key.lower(): value for key, value in headers.items()}
    present = tuple(key for key in ("age", "date", "etag") if key in normalized)
    age = _nonnegative_finite(normalized.get("age", ""))

    max_age: float | None = None
    for directive in normalized.get("cache-control", "").split(","):
        name, separator, value = directive.strip().partition("=")
        if name.lower() == "max-age" and separator:
            max_age = _nonnegative_finite(value.strip().strip('"'))
    warning = normalized.get("warning", "")
    if warning.lstrip().startswith("110 "):
        return "stale", age, present
    if age is not None:
        state = "stale" if max_age is not None and age > max_age else (
            "cached" if age > 0 else "fresh"
        )
        return state, age, present

    date_value = normalized.get("date")
    if date_value:
        try:
            source_date = parsedate_to_datetime(date_value)
            if source_date.tzinfo is None:
                source_date = source_date.replace(tzinfo=UTC)
            current = now or datetime.now(UTC)
            source_age = max(0.0, (current - source_date).total_seconds())
            if max_age is not None:
                state = "stale" if source_age > max_age else "fresh"
            else:
                state = "cached" if source_age > 0 else "fresh"
            return state, source_age, present
        except (TypeError, ValueError, OverflowError):
            return "unknown", None, present
    return "unknown", None, present


def classify_latency_bucket(seconds: float | None) -> str:
    """Classifies elapsed time into fixed, deterministic low-cardinality buckets."""
    if seconds is None: return "unknown"
    for edge, label in zip(
        (10.0, 30.0, 60.0, 300.0, 900.0),
        ("<10s", "10-30s", "30-60s", "1-5m", "5-15m"),
        strict=False,
    ):
        if seconds < edge: return label
    return ">15m"


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts: return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except Exception: return None


@dataclass(frozen=True, slots=True)
class EventLatencyProvenance:
    """Tracks end-to-end event timestamps across publication, observation, commit, and delivery."""

    published_at: str | None = None
    observed_at: str | None = None
    committed_at: str | None = None
    accepted_at: str | None = None

    def compute_publication_lag(self, max_drift_sec: float = 60.0) -> float | None:
        """Measure source-to-observation lag, rejecting missing or implausible dates."""
        pub, obs = _parse_ts(self.published_at), _parse_ts(self.observed_at)
        if not pub or not obs: return None
        diff = (obs - pub).total_seconds()
        if diff < -max_drift_sec or diff > 365 * 86400: return None
        return max(0.0, diff)

    def compute_ingest_lag(self) -> float | None:
        obs, com = _parse_ts(self.observed_at), _parse_ts(self.committed_at)
        return max(0.0, (com - obs).total_seconds()) if obs and com else None

    def compute_delivery_lag(self) -> float | None:
        com, acc = _parse_ts(self.committed_at), _parse_ts(self.accepted_at)
        return max(0.0, (acc - com).total_seconds()) if com and acc else None

    def compute_e2e_lag(self) -> float | None:
        obs, acc = _parse_ts(self.observed_at), _parse_ts(self.accepted_at)
        return max(0.0, (acc - obs).total_seconds()) if obs and acc else None


@dataclass(frozen=True, slots=True)
class PollMetricSample:
    correlation_id: str
    slot_id: str
    status_code: int
    error: bool
    source_visibility: str
    cache_state: str
    cache_age_sec: float | None
    cache_headers: tuple[str, ...]
    publication_lag_sec: float | None
    duration_sec: float
    wire_body_bytes: int | None = None


@dataclass
class LowCardinalityMetricsCollector:
    """Accumulates latency distributions and query states into deterministic buckets."""

    samples: deque[float] = field(default_factory=lambda: deque(maxlen=METRIC_SAMPLE_LIMIT))
    state_counts: dict[str, int] = field(
        default_factory=lambda: {
            "active": 0, "bootstrap": 0, "waiting": 0, "degraded": 0
        }
    )
    poll_samples: deque[PollMetricSample] = field(
        default_factory=lambda: deque(maxlen=METRIC_SAMPLE_LIMIT)
    )
    slot_requests: dict[str, int] = field(default_factory=dict)
    slot_errors: dict[str, int] = field(default_factory=dict)
    queue_age_samples: deque[float] = field(
        default_factory=lambda: deque(maxlen=METRIC_SAMPLE_LIMIT)
    )
    delivery_lag_samples: deque[float] = field(
        default_factory=lambda: deque(maxlen=METRIC_SAMPLE_LIMIT)
    )
    valid_tracker: ValidMetricsTracker = field(default_factory=ValidMetricsTracker)

    def record_poll(
        self,
        *,
        slot_id: str,
        status_code: int,
        error: bool,
        headers: dict[str, str],
        source_visibility: str,
        publication_lag_sec: float | None,
        duration_sec: float,
        wire_body_bytes: int | None = None,
        now: datetime | None = None,
    ) -> PollMetricSample:
        cache_state, cache_age, cache_headers = classify_cache_freshness(headers, now)
        sample = PollMetricSample(
            correlation_id=uuid4().hex,
            slot_id=slot_id,
            status_code=int(status_code),
            error=bool(error),
            source_visibility=source_visibility,
            cache_state=cache_state,
            cache_age_sec=cache_age,
            cache_headers=cache_headers,
            publication_lag_sec=publication_lag_sec,
            duration_sec=max(0.0, duration_sec),
            wire_body_bytes=wire_body_bytes,
        )
        self.poll_samples.append(sample)
        self.slot_requests[slot_id] = self.slot_requests.get(slot_id, 0) + 1
        if error:
            self.slot_errors[slot_id] = self.slot_errors.get(slot_id, 0) + 1
        if publication_lag_sec is not None:
            self.record_latency(publication_lag_sec)
        return sample

    def record_queue_age(self, seconds: float | None) -> None:
        if seconds is not None and math.isfinite(seconds) and seconds >= 0:
            self.queue_age_samples.append(seconds)

    def response_body_summary(self) -> dict[str, int | float | str | None]:
        sizes = [sample.wire_body_bytes for sample in self.poll_samples
                 if sample.wire_body_bytes is not None]
        total = sum(sizes)
        return {
            "scope": "recent_in_memory_samples",
            "samples": len(self.poll_samples),
            "measured_samples": len(sizes),
            "body_bytes": total,
            "mean_body_bytes": total / len(sizes) if sizes else None,
        }

    def record_delivery_provenance(self, provenance: EventLatencyProvenance) -> None:
        lag = provenance.compute_delivery_lag()
        if lag is not None:
            self.delivery_lag_samples.append(lag)

    def record_latency(self, sec: float | None) -> None:
        if sec is not None and not math.isnan(sec) and sec >= 0:
            self.samples.append(sec)

    def record_state(self, state: str) -> None:
        st = state.lower()
        self.state_counts[st if st in self.state_counts else "active"] += 1

    def get_percentile(self, p: float) -> float:
        if not self.samples: return 0.0
        s = sorted(self.samples)
        return round(s[min(int(len(s) * p), len(s) - 1)], 2)

    def get_bucket_distribution(self) -> dict[str, int]:
        dist = {label: 0 for label in LATENCY_BUCKET_LABELS}
        for s in self.samples: dist[classify_latency_bucket(s)] += 1
        return dist
