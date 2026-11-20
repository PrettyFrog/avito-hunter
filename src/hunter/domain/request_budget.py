"""Instance-owned, nonblocking admission state shared by identities on one egress.

No queue or future reservations: deferred polls must be rescheduled, never accumulated.
Unknown proxy topology belongs to the same conservative group, not separate endpoints.
"""
import asyncio
from dataclasses import dataclass, field
import math
import time
from collections.abc import Callable
from uuid import uuid4


@dataclass(frozen=True)
class BudgetPermit:
    token: str
    key: tuple[str, str]


@dataclass(frozen=True)
class BudgetDecision:
    permit: BudgetPermit | None
    reason: str
    not_before: float
    would_defer: bool = False


@dataclass
class _BudgetState:
    not_before: float = 0.0
    cooldown_until: float = 0.0
    active: int = 0
    attempts: int = 0
    completed: int = 0
    changed: asyncio.Event = field(default_factory=asyncio.Event)


class RequestBudget:
    """Call only on its owning event loop; transitions contain no await points.

    Observe records hypothetical deferrals without changing source pacing.
    Enforcement never derives a supposedly safe source limit from observations.
    """

    def __init__(self, *, enforce: bool = False, spacing_sec: float = 0.0,
                 spacing_provider: Callable[[], float] | None = None,
                 shared_upstreams: bool = False,
                 max_active: int = 256, max_cooldown_sec: float | None = None,
                 clock: Callable[[], float] = time.monotonic):
        if not math.isfinite(spacing_sec) or spacing_sec < 0 or max_active < 1:
            raise ValueError("Invalid request budget configuration")
        if max_cooldown_sec is not None and (not math.isfinite(max_cooldown_sec) or max_cooldown_sec < 0):
            raise ValueError("Invalid max_cooldown_sec configuration")
        self.enforce, self.spacing_sec, self.max_active = enforce, spacing_sec, max_active
        self.spacing_provider = spacing_provider
        self.shared_upstreams = shared_upstreams
        self.max_cooldown_sec = max_cooldown_sec
        self.clock = clock
        self._states: dict[tuple[str, str], _BudgetState] = {}
        self._permits: dict[str, BudgetPermit] = {}
        self._closed = False

    @property
    def has_spacing(self) -> bool:
        return self.spacing_sec > 0 or self.spacing_provider is not None

    def _spacing_now(self) -> float:
        value = self.spacing_provider() if self.spacing_provider is not None else self.spacing_sec
        if not math.isfinite(value) or value < 0:
            raise ValueError("Invalid current request spacing")
        return value

    def key(self, egress_group: str | None, upstream: str) -> tuple[str, str]:
        if not upstream:
            raise ValueError("An explicit upstream is required")
        return (egress_group or "unknown", "all" if self.shared_upstreams else upstream)

    def before_attempt(self, egress_group: str | None, upstream: str,
                       *, deadline: float) -> BudgetDecision:
        now = self.clock()
        key = self.key(egress_group, upstream)
        state = self._states.setdefault(key, _BudgetState())
        if self._closed or now >= deadline or math.isnan(deadline):
            return BudgetDecision(None, "closed" if self._closed else "expired", now)
        if len(self._permits) >= self.max_active:
            return BudgetDecision(None, "capacity", now)
        deferred = state.active > 0 or now < state.not_before
        if self.enforce and deferred:
            return BudgetDecision(None, "busy" if state.active else "cooldown", state.not_before, True)
        spacing = self._spacing_now()
        permit = BudgetPermit(uuid4().hex, key)
        self._permits[permit.token] = permit
        state.active += 1
        state.attempts += 1
        state.not_before = max(state.not_before, now + spacing)
        return BudgetDecision(permit, "observe" if not self.enforce else "admitted", now, deferred)

    def after_attempt(self, permit: BudgetPermit, *, retry_after_sec: float | None = None) -> bool:
        if self._permits.get(permit.token) != permit:
            return False
        if retry_after_sec is not None and (not math.isfinite(retry_after_sec) or retry_after_sec < 0):
            raise ValueError("Invalid Retry-After duration")
        del self._permits[permit.token]
        state = self._states[permit.key]
        state.active -= 1
        state.completed += 1
        if retry_after_sec is not None:
            effective_delay = min(retry_after_sec, self.max_cooldown_sec) if self.max_cooldown_sec is not None else retry_after_sec
            state.cooldown_until = max(state.cooldown_until, self.clock() + effective_delay)
            state.not_before = max(state.not_before, state.cooldown_until)
        state.changed.set()
        return True

    def restore_cooldown(self, egress_group: str | None, upstream: str, *, not_before: float) -> None:
        if not math.isfinite(not_before):
            raise ValueError("Invalid cooldown deadline")
        state = self._states.setdefault(self.key(egress_group, upstream), _BudgetState())
        state.cooldown_until = max(state.cooldown_until, not_before)
        state.not_before = max(state.not_before, not_before)
        state.changed.set()

    def change_event(self, egress_group: str | None, upstream: str) -> asyncio.Event:
        return self._states.setdefault(self.key(egress_group, upstream), _BudgetState()).changed

    def cooldown_deadline(self, egress_group: str | None, upstream: str) -> float:
        state = self._states.get(self.key(egress_group, upstream))
        return state.cooldown_until if state is not None else 0.0

    def mark_sent(self, permit: BudgetPermit) -> None:
        if self._permits.get(permit.token) != permit:
            raise ValueError("Inactive wire permit")
        state = self._states[permit.key]
        state.not_before = max(state.not_before, self.clock() + self._spacing_now())
        state.changed.set()

    def snapshot(self, egress_group: str | None, upstream: str) -> dict[str, float | int]:
        state = self._states.get(self.key(egress_group, upstream), _BudgetState())
        return dict(active=state.active, attempts=state.attempts, completed=state.completed,
                    not_before=state.not_before)

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        self._closed = True
        for state in self._states.values():
            state.changed.set()
