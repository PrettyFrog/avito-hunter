"""Native pre-request admission component; callers own preflight and persistence.

The hook runs after connect/TLS but before HTTP. It must never await or perform SQL.
A session must hold NativePrerequisiteHook strongly through native reset/close.
"""
import asyncio
from typing import Any

from curl_cffi import CurlInfo, CurlOpt, ffi, lib

from hunter.domain.request_budget import BudgetPermit, RequestBudget
from hunter.domain.recovery_policy import parse_retry_after
from hunter.transport.metered_http import HttpDeferred
from hunter.transport.native_headers import NativeHeaderObserver


class NativeHopAdmission:
    def __init__(self, budget: RequestBudget, permit: BudgetPermit, *,
                 egress_group: str | None, upstream: str, deadline: float):
        if permit.key != budget.key(egress_group, upstream):
            raise ValueError("Admission scope does not match permit")
        self.budget, self.permit = budget, permit
        self.egress_group, self.upstream, self.deadline = egress_group, upstream, deadline
        self.failure: BaseException | None = None
        self.retry_after_sec: float | None = None
        self._retry_after_by_upstream: dict[str, float] = {}
        self._persisted_deadlines: dict[str, float] = {}
        self._persist_lock = asyncio.Lock()
        self._first = True
        self._closed = False

    def before_request(self) -> int:
        if self._closed or self.failure is not None:
            return 1
        if self.budget.closed:
            self.failure = HttpDeferred("closed", self.budget.clock())
            self.close()
            return 1
        if self.budget.clock() >= self.deadline:
            self.failure = HttpDeferred("expired", self.budget.clock())
            self.close()
            return 1
        cooldown = self.budget.cooldown_deadline(self.egress_group, self.upstream)
        if self.budget.enforce and self.budget.clock() < cooldown:
            self.failure = HttpDeferred("cooldown", cooldown)
            self.close()
            return 1
        if self._first:
            self._first = False
            # Rebase spacing to wire time, rather than a potentially slow connect.
            self.budget.mark_sent(self.permit)
            return 0
        self.budget.after_attempt(self.permit)
        decision = self.budget.before_attempt(self.egress_group, self.upstream, deadline=self.deadline)
        if decision.permit is None:
            self.failure = HttpDeferred(decision.reason, decision.not_before)
            return 1
        self.permit = decision.permit
        return 0

    def mark_wire(self) -> None:
        if not self._closed and self.failure is None:
            self.budget.mark_sent(self.permit)

    def select_upstream(self, upstream: str) -> None:
        if upstream == self.upstream or self._closed or self.failure is not None:
            return
        self.budget.after_attempt(self.permit)
        self.upstream = upstream
        decision = self.budget.before_attempt(self.egress_group, upstream, deadline=self.deadline)
        if decision.permit is None:
            self.failure = HttpDeferred(decision.reason, decision.not_before)
            self.close()
        else:
            self.permit = decision.permit
            self._first = True

    def observe_header(self, data: bytes) -> None:
        if not data.lower().startswith(b"retry-after:"):
            return
        duration = parse_retry_after(data.split(b":", 1)[1].decode("ascii", errors="replace"))
        if duration is not None:
            self.retry_after_sec = max(self.retry_after_sec or 0.0, duration)
            self._retry_after_by_upstream[self.upstream] = max(
                self._retry_after_by_upstream.get(self.upstream, 0.0), self.budget.clock() + duration)
            self.budget.restore_cooldown(self.egress_group, self.upstream,
                                        not_before=self.budget.clock() + duration)

    async def persist_cooldown(self, store: Any, *, generation: int) -> None:
        async with self._persist_lock:
            while pending := [(key, deadline) for key, deadline in self._retry_after_by_upstream.items()
                              if deadline > self._persisted_deadlines.get(key, -1.0)]:
                for upstream, deadline in pending:
                    shared = {"shared_egress": True} if self.budget.shared_upstreams else {}
                    await store.extend(self.egress_group, upstream, generation=generation,
                                       duration_sec=max(0.0, deadline - self.budget.clock()),
                                       **shared)
                    self._persisted_deadlines[upstream] = deadline

    @property
    def needs_flush(self) -> bool:
        return any(deadline > self._persisted_deadlines.get(key, -1.0)
                   for key, deadline in self._retry_after_by_upstream.items())

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self.budget.after_attempt(self.permit)


class NativePrerequisiteHook:
    def __init__(self, curl: Any, admission: NativeHopAdmission, *, resolve_upstream=None, observe_headers=False):
        self.admission, self._curl, self.resolve_upstream = admission, curl, resolve_upstream
        self._observe_headers, self._headers = observe_headers, None
        self._callback = ffi.callback("int(void *, char *, char *, int, int)", self._consult)
        result = lib._curl_easy_setopt(curl._curl, int(CurlOpt.PREREQFUNCTION),
                                      ffi.cast("void *", self._callback))
        if result:
            admission.close()
            raise RuntimeError("Native pre-request hook unavailable")

    def _consult(self, client, remote, local, remote_port, local_port) -> int:
        try:
            if self._observe_headers and self._headers is None:
                self._headers = NativeHeaderObserver(self._curl, self.admission)
            if self.resolve_upstream is not None:
                url = self._curl.getinfo(CurlInfo.EFFECTIVE_URL)
                url = url.decode("utf-8", errors="replace") if isinstance(url, bytes) else url
                self.admission.select_upstream(self.resolve_upstream(url))
            return self.admission.before_request()
        except BaseException as exc:
            # CFFI must not turn a Python exception into an implicit allow (zero).
            self.admission.failure = exc
            self.admission.close()
            return 1

    def release(self) -> None:
        """Caller must finish native remove/reset before dropping these handles."""
        if self._headers is not None:
            self._headers.release()
        self._headers = None
        self._callback = None
        self._curl = None
