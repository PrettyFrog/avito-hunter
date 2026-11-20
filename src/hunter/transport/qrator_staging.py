"""Isolated handshake cookies, reusing the owning client's network transport."""
from __future__ import annotations
import copy
import http.cookiejar
from typing import Any
import httpx
from curl_cffi.requests import AsyncSession
from hunter.transport.qrator_handshake import is_ft_expired


class _BorrowedTransport(httpx.AsyncBaseTransport):
    def __init__(self, owner: httpx.AsyncClient) -> None:
        self.owner = owner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self.owner._transport_for_url(request.url).handle_async_request(request)


class StagedHandshakeClient:
    def __init__(self, owner: Any) -> None:
        self.owner, self._httpx, self._delegate = owner, None, owner
        self.attempt_scope = getattr(owner, 'attempt_scope', None)
        from hunter.transport.cookie_delta import cookie_key, export_cookies
        self.initial_cookie_keys = {cookie_key(c) for c in export_cookies(owner)}
        jar = getattr(owner.cookies, 'jar', None)
        if isinstance(jar, http.cookiejar.CookieJar):
            new_jar = http.cookiejar.CookieJar()
            for c in jar: new_jar.set_cookie(copy.copy(c))
            self.cookies = type(owner.cookies)(new_jar)
        else:
            self.cookies = copy.deepcopy(owner.cookies)
        if isinstance(owner, httpx.AsyncClient):
            self._httpx = httpx.AsyncClient(
                transport=_BorrowedTransport(owner), headers=owner.headers,
                cookies=self.cookies, timeout=owner.timeout,
                event_hooks=owner.event_hooks, follow_redirects=owner.follow_redirects,
            )
            self.cookies = self._httpx.cookies
        elif isinstance(owner, AsyncSession):
            self._delegate = copy.copy(owner)
            self._delegate.cookies = self.cookies
            self.cookies = self._delegate.cookies
        else:
            self._delegate = copy.copy(owner)
            self._delegate.cookies = self.cookies

    async def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        if self._httpx is not None:
            return await getattr(self._httpx, method)(url, **kwargs)
        resp = await getattr(self._delegate, method)(url, **kwargs)
        return resp

    async def post(self, url: str, **kwargs: Any) -> Any: return await self._request('post', url, **kwargs)
    async def get(self, url: str, **kwargs: Any) -> Any: return await self._request('get', url, **kwargs)
    async def close(self) -> None:
        if self._httpx is not None: await self._httpx.aclose()


def profile_cookie_snapshot(client: Any) -> tuple[Any, ...] | None:
    cookies = getattr(client, "cookies", None)
    if isinstance(cookies, dict): return tuple(sorted(cookies.items()))
    jar = getattr(cookies, "jar", None)
    if not isinstance(jar, http.cookiejar.CookieJar): return None
    try:
        return tuple(sorted((c.name, c.value, c.domain, c.path, c.expires, c.secure, c.discard, repr(sorted(c._rest.items()))) for c in jar))
    except (TypeError, AttributeError): return None
