"""Await per-hop admission without restarting the native redirect chain."""
import asyncio
from datetime import timedelta
from urllib.parse import urljoin

from curl_cffi import CurlOpt
from curl_cffi.requests import Headers
from curl_cffi.requests.exceptions import TooManyRedirects

from hunter.transport.http_body_reader import safe_teardown_response
from hunter.transport.metered_http import HttpDeferred
from hunter.transport.native_lifecycle import drain_owned
from hunter.transport.paced_redirects import prepare_paced_request, redirect_step


def _validate_session(session) -> None:
    if session.auth is not None or session.params or session.discard_cookies:
        raise ValueError("Paced sessions require scoped cookies and request-local auth/params")
    if session.retry.count:
        raise ValueError("Paced sessions cannot restart partially completed chains")
    reserved = {CurlOpt.FOLLOWLOCATION, CurlOpt.POSTREDIR, CurlOpt.CUSTOMREQUEST,
                CurlOpt.HTTPGET, CurlOpt.POST, CurlOpt.NOBODY}
    if reserved.intersection(session.curl_options):
        raise ValueError("Paced redirect policy conflicts with native overrides")


async def paced_native_request(session, method: str, url: str, options: dict, send_once):
    _validate_session(session)
    method = method.upper()
    prepared = prepare_paced_request(method, options)
    headers = Headers(session.headers)
    headers.update(prepared["headers"])
    prepared["headers"] = headers
    follow = options.get("allow_redirects")
    follow = session.allow_redirects if follow is None else follow
    if follow not in (True, False):
        raise ValueError("Unsupported paced redirect mode")
    limit = options.get("max_redirects")
    limit = session.max_redirects if limit is None else limit
    url = urljoin(session.base_url or "", str(url))
    prepared["allow_redirects"] = False
    deadline = session.gateway.budget.clock() + session.request_timeout_sec
    try:
        async with asyncio.timeout(max(0.0, deadline - session.gateway.budget.clock())):
            return await _follow_chain(session, method, url, prepared, bool(follow), limit, deadline, send_once)
    except TimeoutError:
        raise HttpDeferred("expired", deadline) from None


async def _follow_chain(session, method, url, options, follow, limit, deadline, send_once):
    history, elapsed, response = [], timedelta(), None
    try:
        while True:
            response = await send_once(method, url, admission_deadline=deadline,
                                       wait_for_window=True, **options)
            elapsed += response.elapsed
            step = redirect_step(method, response, options) if follow else None
            if step is None:
                response.history = history
                response.redirect_count = len(history)
                response.elapsed = elapsed
                return response
            if limit >= 0 and len(history) >= limit:
                raise TooManyRedirects("Paced redirect limit exceeded", response=response)
            history.append(response)
            await drain_owned(safe_teardown_response(response))
            response = None
            method, url, options = step
    except BaseException:
        if response is not None:
            await drain_owned(safe_teardown_response(response))
        raise
