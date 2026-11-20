"""One trace per curl handle; raw debug headers are parsed and never logged."""
from collections.abc import Callable
import time
from typing import Any
from uuid import uuid4

from curl_cffi.curl import CURLINFO_HEADER_IN, CURLINFO_HEADER_OUT

from hunter.transport.http_attempts import emit_safely
from hunter.domain.recovery_policy import parse_retry_after


class CurlAttemptTrace:
    def __init__(self, emit: Callable[[dict[str, Any]], None], clock: Callable[[], float] = time.monotonic) -> None:
        self.emit, self.clock = emit, clock
        self._current: tuple[str, float] | None = None
        self._proxy_connect = False
        self._wire_started = False
        self._status: int | None = None
        self._retry_after: float | None = None
        self._service = "unknown"

    def begin(self) -> None:
        if self._current is not None:
            return
        self._current = (uuid4().hex, self.clock())
        self._wire_started = False
        emit_safely(self.emit, {"event": "http_attempt_start", "attempt_id": self._current[0],
                               "started_mono": self._current[1], "purpose": "poll"})

    def debug(self, kind: int, data: bytes) -> None:
        if kind == CURLINFO_HEADER_OUT:
            self._proxy_connect = data.startswith(b"CONNECT ")
            if not self._proxy_connect:
                if self._wire_started:
                    self.finish()
                self.begin()
                self._wire_started = True
                host = next((line.split(b":", 1)[1].strip().lower() for line in data.splitlines()
                             if line.lower().startswith(b"host:")), b"")
                host = host.split(b":", 1)[0]
                self._service = "avito" if host == b"avito.ru" or host.endswith(b".avito.ru") else "other"
        elif kind == CURLINFO_HEADER_IN and not self._proxy_connect:
            if data.startswith(b"HTTP/"):
                fields = data.split(None, 2)
                if len(fields) > 1 and fields[1].isdigit() and int(fields[1]) >= 200:
                    self._status = int(fields[1])
            elif data.lower().startswith(b"retry-after:"):
                self._retry_after = parse_retry_after(data.split(b":", 1)[1].decode("ascii", errors="replace"))
            elif data.strip() == b"" and self._status is not None:
                self.finish()

    def finish(self, status: int | None = None) -> None:
        if self._current is None:
            return
        attempt_id, started = self._current
        self._current = None
        status = status if status is not None else self._status
        emit_safely(self.emit, {"event": "http_attempt_end", "attempt_id": attempt_id, "purpose": "poll",
                               "status": status, "outcome": "response" if status is not None else "unavailable",
                               "duration_sec": max(0.0, self.clock() - started),
                               "wire_started": self._wire_started,
                               "retry_after_sec": self._retry_after, "target_service": self._service,
                               "phase": "response_headers" if status is not None else "transport_end"})
        self._wire_started = False
        self._status, self._retry_after = None, None
        self._service = "unknown"
