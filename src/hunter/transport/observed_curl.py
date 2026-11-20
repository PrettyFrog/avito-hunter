"""Instrument each curl handle without sharing trace state between requests."""
import os
from collections.abc import Callable
from typing import Any

from curl_cffi import CurlOpt
from curl_cffi.requests import AsyncSession

from hunter.transport.curl_attempts import CurlAttemptTrace
from hunter.transport.attempt_labels import AttemptLabels
from hunter.transport.attempt_scope import AttemptScope
from hunter.transport.http_attempts import log_attempt


class ObservedCurlSession(AsyncSession):
    def __init__(
        self,
        *,
        emit: Callable[[dict[str, Any]], None] | None = None,
        labels: AttemptLabels | None = None,
        buffersize: int = 0,
        pipewait: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._attempt_emit = emit or self._log_event
        if labels is not None:
            self._attempt_emit = labels.emitter(self._attempt_emit)
        self._attempt_traces: dict[Any, CurlAttemptTrace] = {}
        self.attempt_scope = AttemptScope()
        self._buffersize = buffersize or int(os.environ.get("HUNTER_CURL_BUFFERSIZE", "0"))
        self._pipewait = pipewait or (os.environ.get("HUNTER_CURL_PIPEWAIT", "0").lower() in ("1", "true", "yes"))

    @staticmethod
    def _log_event(event: dict[str, Any]) -> None:
        log_attempt(event)

    async def pop_curl(self):
        curl = await super().pop_curl()
        trace = CurlAttemptTrace(self.attempt_scope.capture_emitter(self._attempt_emit))
        try:
            curl.setopt(CurlOpt.DEBUGFUNCTION, trace.debug)
            curl.setopt(CurlOpt.VERBOSE, 1)
            if self._buffersize > 0:
                curl.setopt(CurlOpt.BUFFERSIZE, self._buffersize)
            if self._pipewait:
                curl.setopt(CurlOpt.PIPEWAIT, 1)
        except BaseException:
            self.release_curl(curl)
            raise
        self._attempt_traces[curl] = trace
        trace.begin()
        return curl

    def release_curl(self, curl) -> None:
        # curl_cffi clears this CFFI handle before remove_handle(), which may still
        # invoke DEBUGFUNCTION. Keep the callback alive until native reset/close.
        callback_handle = getattr(curl, "_debug_handle", None)
        try:
            trace = self._attempt_traces.pop(curl, None)
            if trace is not None:
                trace.finish()
            super().release_curl(curl)
        finally:
            del callback_handle
