"""Nonblocking, bounded transport admission: a deferred request never enters the wire."""
import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import httpx

from hunter.domain.request_budget import RequestBudget
from hunter.domain.recovery_policy import parse_retry_after
from hunter.transport.permit_stream import PermitStream

T = TypeVar("T")


class HttpDeferred(Exception):
    """Scheduling result, not a network error and never a reason for immediate retry."""

    def __init__(self, reason: str, not_before: float):
        super().__init__(reason)
        self.reason, self.not_before = reason, not_before


def upstream_for_host(host: str) -> str:
    host = host.lower().rstrip(".")
    return "avito" if host == "avito.ru" or host.endswith(".avito.ru") else "other"


class RequestGateway:
    def __init__(self, budget: RequestBudget, *, cooldown_store: Any = None, max_waiters: int | None = None):
        self.budget, self.cooldown_store = budget, cooldown_store
        self.max_waiters = budget.max_active if max_waiters is None else max_waiters
        if type(self.max_waiters) is not int or self.max_waiters < 1:
            raise ValueError("Invalid admission waiter capacity")
        self._waiting = 0

    @property
    def waiting(self) -> int:
        return self._waiting

    async def restore(self, *, egress_group: str | None, upstream: str):
        if self.cooldown_store is not None:
            sources = (upstream, "other" if upstream == "avito" else "avito") if self.budget.shared_upstreams else (upstream,)
            for source in sources:
                record = await self.cooldown_store.load(egress_group, source)
                if record is not None:
                    self.budget.restore_cooldown(egress_group, source, not_before=record.monotonic_deadline(
                        utc_now=self.cooldown_store.wall_clock(), mono_now=self.budget.clock()))

    async def acquire(self, *, egress_group: str | None, upstream: str, deadline: float, wait: bool = False):
        if self._waiting >= self.max_waiters:
            raise HttpDeferred("admission_capacity", self.budget.clock())
        self._waiting += 1
        try:
            await self.restore(egress_group=egress_group, upstream=upstream)
            while True:
                changed = self.budget.change_event(egress_group, upstream)
                changed.clear()
                decision = self.budget.before_attempt(egress_group, upstream, deadline=deadline)
                if decision.permit is not None:
                    return decision.permit
                if not wait or decision.reason not in {"busy", "cooldown"} or decision.not_before >= deadline:
                    raise HttpDeferred(decision.reason, decision.not_before)
                delay = deadline - self.budget.clock() if decision.reason == "busy" else decision.not_before - self.budget.clock()
                try:
                    await asyncio.wait_for(changed.wait(), timeout=max(0.0, delay))
                except TimeoutError:
                    continue
        finally:
            self._waiting -= 1

    async def run(self, send: Callable[[], Awaitable[T]], *, egress_group: str | None,
                  upstream: str, generation: int, deadline: float) -> T:
        permit = await self.acquire(egress_group=egress_group, upstream=upstream, deadline=deadline)
        retry_after = None
        stream_owns_permit = False
        try:
            response = await send()
            headers = getattr(response, "headers", {}) or {}
            retry_after = next((parse_retry_after(v) for k, v in headers.items() if k.lower() == "retry-after"), None)
            # Install the in-memory prohibition before awaiting durable persistence.
            if retry_after is not None:
                self.budget.restore_cooldown(egress_group, upstream,
                                            not_before=self.budget.clock() + retry_after)
                if self.cooldown_store is not None:
                    try:
                        await self.cooldown_store.extend(egress_group, upstream, generation=generation,
                                                         duration_sec=retry_after, shared_egress=True)
                    except BaseException:
                        if isinstance(response, httpx.Response):
                            await response.aclose()
                        raise
            if isinstance(response, httpx.Response) and not response.is_stream_consumed:
                response.stream = PermitStream(response.stream,
                    lambda: self.budget.after_attempt(permit))
                stream_owns_permit = True
            return response
        finally:
            if not stream_owns_permit:
                self.budget.after_attempt(permit)


class MeteredTransport(httpx.AsyncBaseTransport):
    """Wrap outside AttemptTransport so rejected admission is not counted as HTTP."""

    def __init__(self, inner: httpx.AsyncBaseTransport, gateway: RequestGateway, *,
                 egress_group: str | None, generation: int, timeout_sec: float = 25):
        self.inner, self.gateway = inner, gateway
        self.egress_group, self.generation, self.timeout_sec = egress_group, generation, timeout_sec

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self.gateway.run(lambda: self.inner.handle_async_request(request),
            egress_group=self.egress_group, upstream=upstream_for_host(request.url.host),
            generation=self.generation, deadline=self.gateway.budget.clock() + self.timeout_sec)

    async def aclose(self) -> None:
        await self.inner.aclose()

    async def __aenter__(self):
        await self.inner.__aenter__()
        return self

    async def __aexit__(self, *args):
        await self.inner.__aexit__(*args)


def meter_httpx_client(client: httpx.AsyncClient, gateway: RequestGateway, *,
                       egress_group: str, generation: int, timeout_sec: float = 25) -> None:
    """Wrap every existing HTTPX route after attempt instrumentation, retaining proxy mounts."""
    wrapped = {}
    def wrap(transport):
        if transport is None or isinstance(transport, MeteredTransport):
            return transport
        return wrapped.setdefault(id(transport), MeteredTransport(transport, gateway,
            egress_group=egress_group, generation=generation, timeout_sec=timeout_sec))
    client._transport = wrap(client._transport)
    client._mounts = {pattern: wrap(transport) for pattern, transport in client._mounts.items()}
