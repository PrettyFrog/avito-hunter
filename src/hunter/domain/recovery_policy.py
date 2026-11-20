"""Policy for backoff calculation, HTTP Retry-After headers, and failure cooldowns (T-973)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
import re
import math

from hunter.domain.slots import SlotSignal

_MAX_BACKOFF = 3600.0


def parse_retry_after(raw: str | int | float | None, now: datetime | None = None) -> float | None:
    if raw is None or not (s := str(raw).strip()):
        return None
    if re.fullmatch(r"^\d+(?:\.\d+)?$", s):
        try:
            value = float(s)
            horizon = (datetime.max.replace(tzinfo=UTC) - (now or datetime.now(UTC))).total_seconds()
            return min(value, horizon) if value >= 0 else None
        except (ValueError, TypeError):
            return None
    try:
        dt = parsedate_to_datetime(s)
        dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt
        return max(0.0, (dt - (now or datetime.now(UTC))).total_seconds())
    except Exception:
        return None


def saturated_deadline(now: datetime, duration_sec: float) -> datetime:
    if math.isnan(duration_sec) or duration_sec < 0:
        raise ValueError("Invalid quarantine duration")
    try:
        return now + timedelta(seconds=duration_sec)
    except OverflowError:
        return datetime.max.replace(tzinfo=now.tzinfo or UTC)


def compute_slot_backoff(signal: SlotSignal, errors: int, req: float | None = None, ra: float | None = None) -> float:
    if signal in (SlotSignal.HTTP_429, SlotSignal.HTTP_403, SlotSignal.CHALLENGE_DETECTED):
        val = (62.0, 120.0, 300.0)[min(max(0, errors), 2)]
    elif signal == SlotSignal.TIMEOUT:
        val = 15.0 if errors <= 1 else 45.0
    else:
        val = req if req is not None else _MAX_BACKOFF
    if ra is not None:
        val = max(val, ra)
    if req is not None and req != _MAX_BACKOFF:
        val = max(val, req)
    return max(0.0, val)
