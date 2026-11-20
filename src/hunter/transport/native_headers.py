"""Per-handle header observation including trailers; preserve the library buffer."""
from typing import Any

from curl_cffi import CurlOpt, ffi
from curl_cffi.curl import CURLINFO_HEADER_OUT


class NativeHeaderObserver:
    def __init__(self, curl: Any, admission: Any):
        self.admission = admission
        self._original_handle = curl._header_handle
        if self._original_handle is None:
            raise RuntimeError("Native header buffer is unavailable")
        original = ffi.from_handle(self._original_handle).callback
        self._write = original.write if hasattr(original, "write") else original
        curl.setopt(CurlOpt.HEADERFUNCTION, self.consume)
        # curl_cffi clears its handle before native removal; keep it until reset.
        self._handle = curl._header_handle
        self._original_debug_handle = curl._debug_handle
        self._debug = ffi.from_handle(self._original_debug_handle).callback if self._original_debug_handle else None
        curl.setopt(CurlOpt.DEBUGFUNCTION, self.observe_wire)
        self._debug_handle = curl._debug_handle

    def consume(self, data: bytes) -> int:
        try:
            count = self._write(data)
            self.admission.observe_header(data)
            return count if isinstance(count, int) else len(data)
        except BaseException as exc:
            self.admission.failure = exc
            return 0

    def observe_wire(self, kind: int, data: bytes) -> None:
        try:
            if self._debug is not None:
                self._debug(kind, data)
            if kind == CURLINFO_HEADER_OUT and not data.startswith(b"CONNECT "):
                self.admission.mark_wire()
        except BaseException as exc:
            self.admission.failure = exc

    def release(self) -> None:
        """Drop userdata only after the owning session has completed native reset."""
        self._handle = self._debug_handle = None
        self._original_handle = self._original_debug_handle = None
        self._write = self._debug = None
