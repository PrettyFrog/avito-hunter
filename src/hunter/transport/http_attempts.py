"""Count one physical request invocation without collecting request secrets."""
import asyncio
from collections.abc import Awaitable, Callable
import logging
import json
import time
from typing import Any, TypeVar
from uuid import uuid4

import httpx
from loguru import logger
from hunter.transport.attempt_labels import AttemptLabels
from hunter.transport.attempt_scope import AttemptScope
from hunter.transport.observed_body import ObservedBody
from hunter.domain.recovery_policy import parse_retry_after

T = TypeVar("T")


def log_attempt(event: dict[str, Any]) -> None:
    logger.info("HTTP attempt {}", json.dumps(event, separators=(",", ":")))


_attempt_listeners: list[Callable[[dict[str, Any]], None]] = []


def register_attempt_listener(listener: Callable[[dict[str, Any]], None]) -> None:
    if listener not in _attempt_listeners:
        _attempt_listeners.append(listener)


def unregister_attempt_listener(listener: Callable[[dict[str, Any]], None]) -> None:
    if listener in _attempt_listeners:
        _attempt_listeners.remove(listener)


def emit_safely(emit: Callable[[dict[str, Any]], None], event: dict[str, Any]) -> None:
    try:
        emit(event)
    except Exception:
        logging.getLogger(__name__).warning("HTTP attempt event sink failed")
    for listener in list(_attempt_listeners):
        try:
            listener(event)
        except Exception:
            pass


async def observe_attempt(
    send: Callable[[], Awaitable[T]], emit: Callable[[dict[str, Any]], None], *,
    purpose: str, clock: Callable[[], float] = time.monotonic,
) -> T:
    """Instrument one wire call; caller must invoke separately for redirects/retries.

    Duration ends when send returns (usually headers), not at completion of streamed body.
    No URL, headers, cookies or exception messages are accepted into the event schema.
    """
    if purpose not in {"poll", "warmup", "recovery", "retry", "redirect"}:
        raise ValueError("Unknown HTTP attempt purpose")
    started = clock()
    attempt_id = uuid4().hex
    emit_safely(emit, {"event": "http_attempt_start", "attempt_id": attempt_id, "purpose": purpose,
          "started_mono": started})
    status, outcome, retry_after = None, "error", None
    try:
        response = await send()
        status = getattr(response, "status_code", None)
        headers = getattr(response, "headers", {}) or {}
        retry_after = next((parse_retry_after(v) for k, v in headers.items() if k.lower() == "retry-after"), None)
        outcome = "response"
        if isinstance(response, httpx.Response) and not response.is_stream_consumed:
            def finish_body(body_outcome: str) -> None:
                emit_safely(emit, {"event": "http_attempt_body_end", "attempt_id": attempt_id,
                    "purpose": purpose, "status": status, "outcome": body_outcome,
                    "duration_sec": max(0.0, clock() - started), "phase": "body"})
            response.stream = ObservedBody(response.stream, finish_body)
        return response
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    finally:
        emit_safely(emit, {"event": "http_attempt_end", "attempt_id": attempt_id, "purpose": purpose,
              "status": status, "outcome": outcome, "duration_sec": max(0.0, clock() - started),
              "phase": "send_return", "retry_after_sec": retry_after})


class AttemptTransport(httpx.AsyncBaseTransport):
    """Observe each HTTPX transport invocation, including client-managed redirects."""

    def __init__(self, inner: httpx.AsyncBaseTransport, emit: Callable[[dict[str, Any]], None], scope: AttemptScope) -> None:
        self.inner, self.emit = inner, emit
        self.scope = scope

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host.lower()
        service = "avito" if host == "avito.ru" or host.endswith(".avito.ru") else "other"
        sink = self.scope.capture_emitter(lambda event: self.emit({**event, "target_service": service}))
        return await observe_attempt(lambda: self.inner.handle_async_request(request), sink, purpose="poll")

    async def aclose(self) -> None:
        await self.inner.aclose()

    async def __aenter__(self) -> "AttemptTransport":
        await self.inner.__aenter__()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.inner.__aexit__(*args)


def instrument_httpx_client(client: httpx.AsyncClient, emit: Callable[[dict[str, Any]], None], *, labels: AttemptLabels | None = None) -> None:
    """Wrap existing routes without rebuilding proxy/environment configuration.

    HTTPX has no public middleware installer. These private fields are covered by
    routing/ownership tests; instrument only a newly constructed client in production.
    """
    emit = labels.emitter(emit) if labels is not None else emit
    scope = client._transport.scope if isinstance(client._transport, AttemptTransport) else AttemptScope()
    client.attempt_scope = scope
    wrapped: dict[int, AttemptTransport] = {}
    def wrap(transport: httpx.AsyncBaseTransport) -> AttemptTransport:
        if isinstance(transport, AttemptTransport):
            return transport
        return wrapped.setdefault(id(transport), AttemptTransport(transport, emit, scope))
    client._transport = wrap(client._transport)
    client._mounts = {pattern: wrap(transport) if transport is not None else None
                      for pattern, transport in client._mounts.items()}


def observed_httpx_client(*, labels: AttemptLabels | None = None, **kwargs: Any) -> httpx.AsyncClient:
    client = httpx.AsyncClient(**kwargs)
    instrument_httpx_client(client, log_attempt, labels=labels)
    return client
