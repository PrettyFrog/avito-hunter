"""The interval floor must follow request headers, not only the earlier PREREQ."""
from io import BytesIO

import pytest
from curl_cffi import Curl, CurlOpt, ffi
from curl_cffi.curl import CURLINFO_HEADER_OUT

from hunter.domain.request_budget import RequestBudget
from hunter.transport.native_admission import NativeHopAdmission
from hunter.transport.native_headers import NativeHeaderObserver


@pytest.mark.parametrize("headers,expected", [(b"GET / HTTP/1.1\r\n\r\n", 21),
                                              (b"CONNECT example.test:443 HTTP/1.1\r\n\r\n", 11)])
def test_wire_clock_rebases_spacing_but_proxy_connect_does_not(headers, expected):
    now = [10.0]
    budget = RequestBudget(enforce=True, spacing_sec=1, clock=lambda: now[0])
    permit = budget.before_attempt("shared", "avito", deadline=100).permit
    gate = NativeHopAdmission(budget, permit, egress_group="shared", upstream="avito", deadline=100)
    assert gate.before_request() == 0
    curl = Curl()
    def original(kind, data):
        now[0] = 20
    curl.setopt(CurlOpt.HEADERDATA, BytesIO())
    curl.setopt(CurlOpt.DEBUGFUNCTION, original)
    observer = NativeHeaderObserver(curl, gate)
    try:
        callback = ffi.from_handle(curl._debug_handle).callback
        callback(CURLINFO_HEADER_OUT, headers)
        assert budget.snapshot("shared", "avito")["not_before"] == expected
    finally:
        curl.close()
        gate.close()
        observer.release()
        del observer
