"""Pure domain policy for public Day/Night poll intervals and slot offsets (S5 / T-497).

Contracts:
- Timezone: Europe/Moscow.
- Day: 07:00:00 to 00:59:59.999999 MSK -> 30.0 seconds cycle interval.
- Night: 01:00:00 to 06:59:59.999999 MSK -> 180.0 seconds cycle interval.
- Explicit clock injection (no implicit current time required).
- Slot query capacity: <= 4 queries per slot, evenly spaced offsets.
- Pure decision functions without network or database dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from enum import Enum
import random
from typing import Any, Callable
from zoneinfo import ZoneInfo

DEFAULT_TIMEZONE = "Europe/Moscow"
DAY_INTERVAL_SEC = 30.0
NIGHT_INTERVAL_SEC = 180.0
MAX_SLOT_QUERIES = 4
DAY_START_HOUR = 7
NIGHT_START_HOUR = 1


class PollPeriod(str, Enum):
    DAY = "day"
    NIGHT = "night"


@dataclass(frozen=True, slots=True)
class PollScheduleWindow:
    period: PollPeriod
    cycle_interval_sec: float
    slot_offsets: tuple[float, ...]
    effective_time_msk: datetime


class PollPolicy:
    """Calculates deterministic day/night poll intervals and query offsets."""

    def __init__(
        self,
        day_interval_sec: float = DAY_INTERVAL_SEC,
        night_interval_sec: float = NIGHT_INTERVAL_SEC,
        day_start_hour: int = DAY_START_HOUR,
        night_start_hour: int = NIGHT_START_HOUR,
        tz_name: str = DEFAULT_TIMEZONE,
        max_queries_per_slot: int = MAX_SLOT_QUERIES,
        jitter_ratio: float = 0.0,
        max_jitter_sec: float = 180.0,
        random_func: Callable[[float, float], float] | None = None,
    ) -> None:
        if day_interval_sec <= 0:
            raise ValueError(f"day_interval_sec must be positive, got {day_interval_sec}")
        if night_interval_sec <= 0:
            raise ValueError(f"night_interval_sec must be positive, got {night_interval_sec}")
        if max_queries_per_slot <= 0:
            raise ValueError(f"max_queries_per_slot must be positive, got {max_queries_per_slot}")
        if jitter_ratio < 0:
            raise ValueError(f"jitter_ratio must be non-negative, got {jitter_ratio}")
        if max_jitter_sec <= 0:
            raise ValueError(f"max_jitter_sec must be positive, got {max_jitter_sec}")

        self.day_interval_sec = float(day_interval_sec)
        self.night_interval_sec = float(night_interval_sec)
        self.day_start_hour = int(day_start_hour)
        self.night_start_hour = int(night_start_hour)
        self.tz_name = tz_name
        self.tz = ZoneInfo(tz_name)
        self.max_queries_per_slot = int(max_queries_per_slot)
        self.jitter_ratio = float(jitter_ratio)
        self.max_jitter_sec = float(max_jitter_sec)
        self._random_func = random_func or random.uniform

    def to_msk(self, dt: datetime | None = None) -> datetime:
        """Converts any datetime (UTC, naive, or other tz) to MSK."""
        if dt is None:
            return datetime.now(self.tz)
        if dt.tzinfo is None:
            # Assume naive datetime is in UTC for hermetic testing
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(self.tz)

    def is_day(self, dt: datetime | None = None) -> bool:
        """Returns True if MSK time falls in the day window (07:00 - 01:00 MSK).

        Night window is [01:00:00, 07:00:00).
        """
        msk = self.to_msk(dt)
        t = msk.time()
        night_start = time(self.night_start_hour, 0)
        day_start = time(self.day_start_hour, 0)

        # Night is strictly [01:00, 07:00)
        if night_start <= t < day_start:
            return False
        return True

    def get_period(self, dt: datetime | None = None) -> PollPeriod:
        return PollPeriod.DAY if self.is_day(dt) else PollPeriod.NIGHT

    def get_cycle_interval(self, dt: datetime | None = None) -> float:
        return self.day_interval_sec if self.is_day(dt) else self.night_interval_sec

    def calculate_jitter(self, base_interval: float | None = None, dt: datetime | None = None) -> float:
        """Calculate signed jitter delta bounded by max_jitter_sec."""
        if self.jitter_ratio <= 0:
            return 0.0
        interval = self.get_cycle_interval(dt) if base_interval is None else float(base_interval)
        max_delta = min(interval * self.jitter_ratio, self.max_jitter_sec)
        if max_delta <= 0:
            return 0.0
        return float(self._random_func(-max_delta, max_delta))

    def get_jittered_cycle_interval(self, dt: datetime | None = None) -> float:
        """Returns cycle interval with bounded jitter, keeping interval >= min bound."""
        interval = self.get_cycle_interval(dt)
        delta = self.calculate_jitter(interval, dt)
        return max(interval * 0.1, interval + delta)

    def seconds_until_transition(self, dt: datetime) -> float:
        """Wall-clock time to the next Moscow day/night boundary."""
        local = self.to_msk(dt)
        candidates = []
        for hour in (self.day_start_hour, self.night_start_hour):
            boundary = datetime.combine(local.date(), time(hour), tzinfo=self.tz)
            if boundary <= local:
                boundary += timedelta(days=1)
            candidates.append((boundary - local).total_seconds())
        return min(candidates)

    def calculate_slot_offsets(
        self,
        num_queries: int = MAX_SLOT_QUERIES,
        dt: datetime | None = None,
    ) -> tuple[float, ...]:
        """Calculate evenly distributed offsets for queries assigned to a slot."""
        count = min(max(int(num_queries), 0), self.max_queries_per_slot)
        if count == 0:
            return ()
        cycle = self.get_cycle_interval(dt)
        step = cycle / count
        return tuple(round(i * step, 3) for i in range(count))

    def get_schedule_window(
        self,
        num_queries: int = MAX_SLOT_QUERIES,
        dt: datetime | None = None,
    ) -> PollScheduleWindow:
        msk = self.to_msk(dt)
        period = self.get_period(msk)
        interval = self.get_cycle_interval(msk)
        offsets = self.calculate_slot_offsets(num_queries, msk)
        return PollScheduleWindow(
            period=period,
            cycle_interval_sec=interval,
            slot_offsets=offsets,
            effective_time_msk=msk,
        )

    @classmethod
    def from_config(
        cls,
        cfg: Any,
        random_func: Callable[[float, float], float] | None = None,
    ) -> PollPolicy:
        return cls(
            day_interval_sec=float(getattr(cfg, "day_interval_sec", DAY_INTERVAL_SEC)),
            night_interval_sec=float(getattr(cfg, "night_interval_sec", NIGHT_INTERVAL_SEC)),
            day_start_hour=int(getattr(cfg, "day_start_hour", DAY_START_HOUR)),
            night_start_hour=int(getattr(cfg, "night_start_hour", NIGHT_START_HOUR)),
            tz_name=str(getattr(cfg, "timezone", DEFAULT_TIMEZONE)),
            max_queries_per_slot=int(
                getattr(
                    cfg, "slot_capacity", getattr(cfg, "max_queries_per_slot", MAX_SLOT_QUERIES)
                )
            ),
            jitter_ratio=float(getattr(cfg, "jitter_ratio", 0.0)),
            max_jitter_sec=float(getattr(cfg, "max_jitter_sec", 180.0)),
            random_func=random_func,
        )
