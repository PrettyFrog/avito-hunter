"""Application ports defining core protocol boundaries."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from hunter.domain.events import Event
from hunter.domain.observations import ObservationBatch


@runtime_checkable
class Clock(Protocol):
    """Deterministic time source protocol."""

    def now_utc(self) -> datetime: ...
    def timestamp(self) -> float: ...
    def isoformat(self) -> str: ...
    async def sleep(self, seconds: float) -> None: ...


@runtime_checkable
class Fetcher(Protocol):
    """Network fetch boundary protocol."""

    async def fetch(self, slot_id: str, url: str) -> dict[str, Any] | str: ...


@runtime_checkable
class ObservationStore(Protocol):
    """Write-side observation storage protocol."""

    async def submit_batch(self, batch: ObservationBatch) -> int: ...
    async def get_latest_batch(self, query_id: str) -> ObservationBatch | None: ...


@runtime_checkable
class Delivery(Protocol):
    """Outbound event delivery protocol."""

    async def deliver(self, event: Event, recipient_id: str) -> bool: ...


@runtime_checkable
class SessionStore(Protocol):
    """Hermetic slot session/cookie storage protocol."""

    async def get_session(self, slot_id: str) -> dict[str, Any] | None: ...
    async def save_session(self, slot_id: str, session_data: dict[str, Any]) -> None: ...
    async def invalidate_session(self, slot_id: str) -> None: ...
