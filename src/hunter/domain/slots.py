"""Slot domain states, pure transitions, and capacity controls (S5 / T-476).

Contracts:
- States: READY, SUSPECT, QUARANTINED, AUTH_REQUIRED, CHALLENGE, WARMING, DISABLED.
- Pure transitions: no I/O, deterministic next state from (current, signal, timestamp).
- Strict capacity: <= 4 active queries per healthy slot. 5th query rejected.
- Reserve slots do not accept direct query assignments.
- Day (07:00-01:00 MSK): 30s. Night (01:00-07:00 MSK): 180s. Legacy timings independent.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import Enum
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

MAX_QUERIES_PER_SLOT = 4
MSK_TZ = ZoneInfo("Europe/Moscow")


def validate_slot_id(slot_id: str) -> None:
    """Reject identifiers that would alias another slot's session filename."""
    if not isinstance(slot_id, str) or not slot_id or any(
        not (char.isalnum() or char in "-_") for char in slot_id
    ):
        raise ValueError("Slot ID must contain only letters, numbers, hyphens, or underscores")


def validate_proxy_endpoint(endpoint: str) -> None:
    """Accept only proxy origin URLs without credentials or opaque parameters, or 'direct'."""
    if endpoint in {"direct", "direct://"}:
        return
    try:
        parsed = urlsplit(endpoint)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            raise ValueError
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("Proxy endpoint must be an HTTP(S) origin without credentials") from exc


class SlotHealth(str, Enum):
    READY = "READY"
    SUSPECT = "SUSPECT"
    QUARANTINED = "QUARANTINED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    CHALLENGE = "CHALLENGE"
    WARMING = "WARMING"
    DISABLED = "DISABLED"


class SlotRole(str, Enum):
    ACTIVE = "active"
    RESERVE = "reserve"


class SlotSignal(str, Enum):
    PROBE_OK = "probe_ok"
    HTTP_429 = "http_429"
    HTTP_403 = "http_403"
    TIMEOUT = "timeout"
    CHALLENGE_DETECTED = "challenge_detected"
    CHALLENGE_SOLVED = "challenge_solved"
    AUTH_INVALID = "auth_invalid"
    ADMIN_DISABLE = "admin_disable"
    ADMIN_ENABLE = "admin_enable"
    FAILOVER_TRANSFER = "failover_transfer"


class SlotCapacityError(Exception):
    pass


class CapacityExceededError(SlotCapacityError):
    pass


class ReserveAdmissionError(SlotCapacityError):
    pass


@dataclass(frozen=True, slots=True)
class SlotCapacity:
    max_queries: int = MAX_QUERIES_PER_SLOT
    assigned_queries: tuple[str, ...] = ()

    @property
    def current_load(self) -> int:
        return len(self.assigned_queries)

    @property
    def is_full(self) -> bool:
        return self.current_load >= self.max_queries

    def can_assign(self) -> bool:
        return not self.is_full

    def assign(self, query_id: str) -> SlotCapacity:
        if not self.can_assign():
            raise CapacityExceededError(
                f"Slot capacity exceeded: {self.current_load}/{self.max_queries} queries already assigned"
            )
        if query_id in self.assigned_queries:
            return self
        return SlotCapacity(self.max_queries, self.assigned_queries + (query_id,))

    def release(self, query_id: str) -> SlotCapacity:
        if query_id not in self.assigned_queries:
            return self
        new_assigned = tuple(q for q in self.assigned_queries if q != query_id)
        return SlotCapacity(self.max_queries, new_assigned)


@dataclass(frozen=True, slots=True)
class SlotSnapshot:
    slot_id: str
    endpoint: str = field(repr=False)
    health: SlotHealth
    role: SlotRole
    session_version: int = 1
    generation: int = 1
    capacity: SlotCapacity = field(default_factory=SlotCapacity)
    quarantined_until: str | None = None
    challenge_deadline: str | None = None
    consecutive_errors: int = 0
    consecutive_successes: int = 0
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "slot_id": self.slot_id,
            "endpoint": self.endpoint,
            "health": self.health.value,
            "role": self.role.value,
            "session_version": self.session_version,
            "generation": self.generation,
            "capacity": {
                "max_queries": self.capacity.max_queries,
                "assigned_queries": list(self.capacity.assigned_queries),
            },
            "quarantined_until": self.quarantined_until,
            "challenge_deadline": self.challenge_deadline,
            "consecutive_errors": self.consecutive_errors,
            "consecutive_successes": self.consecutive_successes,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SlotSnapshot:
        cap_raw = data.get("capacity", {})
        cap = SlotCapacity(
            max_queries=int(cap_raw.get("max_queries", MAX_QUERIES_PER_SLOT)),
            assigned_queries=tuple(cap_raw.get("assigned_queries", ())),
        )
        return cls(
            slot_id=str(data["slot_id"]),
            endpoint=str(data.get("endpoint", "")),
            health=SlotHealth(data.get("health", SlotHealth.WARMING.value)),
            role=SlotRole(data.get("role", SlotRole.ACTIVE.value)),
            session_version=int(data.get("session_version", 1)),
            generation=int(data.get("generation", 1)),
            capacity=cap,
            quarantined_until=data.get("quarantined_until"),
            challenge_deadline=data.get("challenge_deadline"),
            consecutive_errors=int(data.get("consecutive_errors", 0)),
            consecutive_successes=int(data.get("consecutive_successes", 0)),
            updated_at=str(data.get("updated_at", "")),
        )


def assign_query_to_slot(slot: SlotSnapshot, query_id: str) -> SlotSnapshot:
    """Assigns query to active slot respecting reserve admission rules."""
    if slot.role == SlotRole.RESERVE:
        raise ReserveAdmissionError(
            f"Cannot assign user query '{query_id}' to reserve slot '{slot.slot_id}': reserve admission prohibited"
        )
    if slot.health not in (SlotHealth.READY, SlotHealth.WARMING, SlotHealth.SUSPECT):
        raise SlotCapacityError(f"Cannot assign query to slot '{slot.slot_id}' in state {slot.health.value}")

    new_cap = slot.capacity.assign(query_id)
    return SlotSnapshot(
        slot_id=slot.slot_id,
        endpoint=slot.endpoint,
        health=slot.health,
        role=slot.role,
        session_version=slot.session_version,
        generation=slot.generation,
        capacity=new_cap,
        quarantined_until=slot.quarantined_until,
        challenge_deadline=slot.challenge_deadline,
        consecutive_errors=slot.consecutive_errors,
        consecutive_successes=slot.consecutive_successes,
        updated_at=slot.updated_at,
    )


def transition_slot_health(
    slot: SlotSnapshot,
    signal: SlotSignal,
    *,
    timestamp: str,
    deadline: str | None = None,
    new_generation: int | None = None,
) -> SlotSnapshot:
    """Pure deterministic slot health transition function."""
    gen = new_generation if new_generation is not None else slot.generation
    if signal == SlotSignal.ADMIN_DISABLE:
        return replace(slot, health=SlotHealth.DISABLED, generation=gen,
                       quarantined_until=None, challenge_deadline=None,
                       consecutive_errors=0, consecutive_successes=0, updated_at=timestamp)
    if slot.health == SlotHealth.DISABLED and signal != SlotSignal.ADMIN_ENABLE:
        return slot
    if signal == SlotSignal.ADMIN_ENABLE:
        return replace(slot, health=SlotHealth.WARMING, generation=gen,
                       quarantined_until=None, challenge_deadline=None,
                       consecutive_errors=0, consecutive_successes=0, updated_at=timestamp)
    if signal == SlotSignal.CHALLENGE_DETECTED:
        return replace(slot, health=SlotHealth.CHALLENGE, generation=gen,
                       quarantined_until=None, challenge_deadline=deadline,
                       consecutive_errors=slot.consecutive_errors + 1,
                       consecutive_successes=0, updated_at=timestamp)
    if signal == SlotSignal.CHALLENGE_SOLVED:
        return replace(slot, health=SlotHealth.READY, generation=gen,
                       session_version=slot.session_version + 1, quarantined_until=None,
                       challenge_deadline=None, consecutive_errors=0,
                       consecutive_successes=1, updated_at=timestamp)
    if signal == SlotSignal.AUTH_INVALID:
        return replace(slot, health=SlotHealth.AUTH_REQUIRED, generation=gen,
                       quarantined_until=None, challenge_deadline=None,
                       consecutive_errors=slot.consecutive_errors + 1,
                       consecutive_successes=0, updated_at=timestamp)
    if signal in (SlotSignal.HTTP_429, SlotSignal.HTTP_403, SlotSignal.TIMEOUT):
        return _transition_http_failure(slot, signal, gen, timestamp, deadline)
    if signal == SlotSignal.PROBE_OK:
        return _transition_probe_success(slot, gen, timestamp)
    return slot


def _transition_http_failure(
    slot: SlotSnapshot, signal: SlotSignal, generation: int, timestamp: str,
    deadline: str | None,
) -> SlotSnapshot:
    errors = slot.consecutive_errors + 1
    health = SlotHealth.QUARANTINED if errors >= 2 or signal == SlotSignal.HTTP_403 else SlotHealth.SUSPECT
    return replace(slot, health=health, generation=generation, quarantined_until=deadline,
                   challenge_deadline=None, consecutive_errors=errors,
                   consecutive_successes=0, updated_at=timestamp)


def _transition_probe_success(slot: SlotSnapshot, generation: int, timestamp: str) -> SlotSnapshot:
    successes = slot.consecutive_successes + 1
    health = SlotHealth.WARMING if slot.health == SlotHealth.WARMING and successes < 2 else SlotHealth.READY
    return replace(slot, health=health, generation=generation, quarantined_until=None,
                   challenge_deadline=None, consecutive_errors=0,
                   consecutive_successes=successes, updated_at=timestamp)


def get_public_poll_interval(dt: datetime | None = None) -> int:
    """Returns public interval in seconds: 30s day (07:00-01:00 MSK), 180s night (01:00-07:00 MSK)."""
    current_dt = dt or datetime.now(UTC)
    msk_dt = current_dt.astimezone(MSK_TZ)
    hour = msk_dt.hour
    if 1 <= hour < 7:
        return 180
    return 30
