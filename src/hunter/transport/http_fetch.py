"""Persistent per-slot async HTTP fetcher with pinned proxies, cookies, and explicit bounds (S5 / T-479).
Contracts:
- Persistent async HTTP client per slot with isolated connection pool and cookie jar.
- Explicit bounds: connect/read timeout, body size limit (prevent OOM), max redirects.
- No browser startup on successful HTTP fetch; supports offline MockTransport tests.
"""

from __future__ import annotations

from hunter.transport.metered_http import HttpDeferred

import asyncio
import os
import sys
import time
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Mapping
from urllib.parse import urlparse, urlunparse
import httpx
from hunter.transport.http_attempts import observed_httpx_client, AttemptLabels
from hunter.transport.browser_headers import _default_browser_headers
from hunter.domain.query_url import is_allowed_fetch_url
from hunter.domain.slots import validate_proxy_endpoint
from hunter.transport.proxy_credentials import ProxyCredentials, ProxyCredentialStore
from hunter.transport.response_metrics import declared_wire_body_bytes
from hunter.transport.session_store import EndpointMismatchError, SessionStore
from hunter.transport.http_body_reader import (
    BodySizeLimitExceededError, HttpFetchError, read_response_content, safe_teardown_response,
)
class HttpTimeoutError(HttpFetchError): pass
class UnsafeFetchUrlError(HttpFetchError): pass

async def _validate_request_url(request: httpx.Request) -> None:
    if not is_allowed_fetch_url(str(request.url)): raise UnsafeFetchUrlError("Unsafe request URL")


def platform_firefox_user_agent() -> str:
    plat = "X11; Linux x86_64" if sys.platform.startswith("linux") else ("Macintosh; Intel Mac OS X 10.15" if sys.platform == "darwin" else "Windows NT 10.0; Win64; x64")
    return f"Mozilla/5.0 ({plat}; rv:133.0) Gecko/20100101 Firefox/133.0"


DEFAULT_FIREFOX_USER_AGENT = platform_firefox_user_agent()
DEFAULT_CHROME_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
DEFAULT_BROWSER_USER_AGENT = os.environ.get("HUNTER_USER_AGENT", DEFAULT_FIREFOX_USER_AGENT)


def _format_proxy_url(endpoint: str, creds: ProxyCredentials | None) -> str | None:
    if not endpoint or endpoint in {"direct", "direct://"}: return None
    if not creds: return endpoint
    p = urlparse(endpoint)
    return urlunparse((p.scheme, f"{creds.username}:{creds.password}@{p.hostname}:{p.port}", p.path, p.params, p.query, p.fragment))


@dataclass(frozen=True, slots=True)
class SlotFetchConfig:
    timeout_sec: float = 25.0
    connect_timeout_sec: float = 5.0
    max_body_bytes: int = 5 * 1024 * 1024  # 5 MB
    max_redirects: int = 5
    user_agent: str = DEFAULT_BROWSER_USER_AGENT
    base_url: str | None = None
    interleave_sessions: bool = os.environ.get("HUNTER_INTERLEAVE_SESSIONS", "1").lower() in ("1", "true")


@dataclass(frozen=True, slots=True)
class SlotFetchResponse:
    status_code: int
    url: str
    text: str
    headers: dict[str, str] = field(default_factory=dict)
    cookies: dict[str, str] = field(default_factory=dict)
    elapsed_sec: float = 0.0
    error: str | None = None
    wire_body_bytes: int | None = None
    client_identity: str = ""
    request_headers: dict[str, str] = field(default_factory=dict)

    @property
    def is_success(self) -> bool: return 200 <= self.status_code < 300 and self.error is None
    @property
    def is_blocked(self) -> bool: return self.status_code == 403
    @property
    def is_rate_limited(self) -> bool: return self.status_code == 429
    @property
    def is_redirect(self) -> bool: return 300 <= self.status_code < 400


class HttpSlotFetcher:
    """Persistent async HTTP client pinned to a single slot, proxy, and cookie store."""

    def __init__(
        self,
        slot_id: str,
        endpoint: str | None = None,
        config: SlotFetchConfig | None = None,
        session_store: SessionStore | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        proxy_credentials: ProxyCredentials | None = None, pow_executor: Any = None,
        gateway: Any = None, egress_group: str = "default",
    ) -> None:
        if endpoint and endpoint not in {"direct", "direct://"}:
            try: validate_proxy_endpoint(endpoint)
            except ValueError as exc: raise HttpFetchError("Unsupported or unsafe slot proxy endpoint") from exc
        self.slot_id, self.endpoint = slot_id, endpoint
        self.config = config or SlotFetchConfig()
        self.session_store, self.custom_transport, self.pow_executor = session_store, transport, pow_executor
        if transport is None:
            from hunter.transport.session_identity import validate_native_user_agent
            validate_native_user_agent(self.config.user_agent)
        headers = _default_browser_headers(self.config.user_agent)
        timeout = httpx.Timeout(self.config.timeout_sec, connect=self.config.connect_timeout_sec)
        client_kwargs: dict[str, Any] = {
            "headers": headers, "timeout": timeout, "follow_redirects": True,
            "max_redirects": self.config.max_redirects,
            "event_hooks": {"request": [self._check_req_url]},
        }
        if transport is not None: client_kwargs["transport"] = transport
        elif endpoint and endpoint not in {"direct", "direct://"}:
            client_kwargs["proxy"] = (
                httpx.Proxy(endpoint, auth=(proxy_credentials.username, proxy_credentials.password))
                if proxy_credentials else endpoint
            )
        self.client: httpx.AsyncClient = observed_httpx_client(labels=AttemptLabels(slot_id, "user_a"), **client_kwargs)
        if gateway is not None:
            from hunter.transport.metered_http import meter_httpx_client
            meter_httpx_client(self.client, gateway, egress_group=egress_group, generation=0)
        self.curl_client, self.curl_client_b = None, None
        self.platform_profile, self._session_index, self._user_b_ready = "linux", 0, False
        self._identity_quarantine_until: dict[str, float] = {}
        if transport is None:
            try:
                from hunter.transport.observed_curl import ObservedCurlSession as CurlSession
                if gateway is not None:
                    from hunter.transport.metered_curl import MeteredCurlSession as CurlSession
                proxy_str = _format_proxy_url(endpoint or "", proxy_credentials)
                impersonate = "firefox133" if "Firefox" in self.config.user_agent else "chrome131"
                gated = {"gateway": gateway, "egress_group": egress_group, "generation": 0, "request_timeout_sec": self.config.timeout_sec} if gateway else {}
                self.curl_client = CurlSession(
                    impersonate=impersonate, headers=headers, proxy=proxy_str, max_redirects=self.config.max_redirects, labels=AttemptLabels(slot_id, "user_a", egress_group), **gated,
                )
                if self.config.interleave_sessions:
                    imp_b = "chrome131" if impersonate == "firefox133" else "firefox133"
                    ua_b = DEFAULT_FIREFOX_USER_AGENT if imp_b == "firefox133" else DEFAULT_CHROME_USER_AGENT
                    self.curl_client_b = CurlSession(
                        impersonate=imp_b, headers=_default_browser_headers(ua_b), proxy=proxy_str, max_redirects=self.config.max_redirects, labels=AttemptLabels(slot_id, "user_b", egress_group), **gated,
                    )
            except Exception:
                if gateway is not None: raise
                self.curl_client, self.curl_client_b = None, None

    async def _check_req_url(self, req: httpx.Request) -> None:
        if not (self.config.base_url and str(req.url).startswith(self.config.base_url)):
            await _validate_request_url(req)

    def _get_active_client(self) -> Any:
        return self.curl_client if self.curl_client is not None else self.client

    async def load_cookies_from_store(self) -> int:
        """Loads pinned session cookies for this slot into the HTTP client's cookie jar."""
        from hunter.transport.session_identity import hydrate_identity
        if not self.session_store: return 0
        try: payload = await self.session_store.load_session(self.slot_id, expected_endpoint=self.endpoint or "")
        except EndpointMismatchError:
            if not self.endpoint: raise
            payload = None
        except Exception: payload = None
        accepted = hydrate_identity(self, "user_a", payload)
        count = len(payload.cookies) if accepted else 0
        if self.curl_client_b is not None:
            try: profile = await self.session_store.load_session(f"{self.slot_id}__user_b", expected_endpoint=self.endpoint or "")
            except Exception: profile = None
            hydrate_identity(self, "user_b", profile)
        return count

    async def save_user_b_cookies_to_store(self, fence: tuple[int, int] | None = None) -> None:
        from hunter.transport.session_profile_io import save_user_b_profile
        await save_user_b_profile(self, fence)

    def reset_identity_in_memory(self, identity: str) -> None:
        from hunter.transport.session_reset import reset_fetcher_identity
        reset_fetcher_identity(self, identity)

    async def load_identity_from_store(self, identity: str) -> int:
        from hunter.transport.session_profile_io import load_identity_profile
        return await load_identity_profile(self, identity)

    async def save_cookies_to_store(self, version: int = 1, generation: int = 1) -> None:
        """Persists current cookie jar into the session store."""
        if not self.session_store: return
        target = self._get_active_client()
        if target is not None:
            from hunter.transport.recovery_transport import DISALLOWED_COOKIE_NAMES
            cookie_list = [
                {"name": k, "value": v, "domain": c.domain, "path": c.path}
                for c in target.cookies.jar for k, v in [(c.name, c.value)]
                if k not in DISALLOWED_COOKIE_NAMES
            ]
            ua = target.headers.get("User-Agent") if hasattr(target, "headers") else None
            try: existing_a = await self.session_store.load_session(self.slot_id)
            except Exception: existing_a = None
            m_a = getattr(existing_a, "metadata", None)
            ver_a = version if not m_a else max(version, m_a.version)
            gen_a = generation if not m_a else (m_a.generation + 1 if ver_a == m_a.version else generation)
            await self.session_store.save_session(
                slot_id=self.slot_id, endpoint=self.endpoint or "", version=ver_a,
                generation=gen_a, cookies=cookie_list, storage_state={"user_agent": ua} if ua else {},
            )
        await self.save_user_b_cookies_to_store()

    async def _read_stream_body(self, resp: httpx.Response) -> str:
        _, text = await read_response_content(resp, self.config.max_body_bytes)
        return text

    async def _warmup_b_if_needed(self, curl: Any, referer: str) -> bool:
        if curl is self.curl_client_b and not self._user_b_ready:
            from hunter.transport.catalog_api import warmup_client_session
            from hunter.transport.session_profile_io import prepare_user_b_warmup
            from hunter.transport.session_store import StaleSessionVersionError
            try:
                fence = await prepare_user_b_warmup(self)
            except StaleSessionVersionError:
                return False
            ok = await warmup_client_session(curl, referer, executor=self.pow_executor)
            if ok:
                try:
                    await self.save_user_b_cookies_to_store(fence)
                except StaleSessionVersionError:
                    await prepare_user_b_warmup(self)
                    self._user_b_ready = False
                    return False
                self._user_b_ready = True
                return True
            self._user_b_ready = False
            return False
        return True

    def _sanitize_identity_headers(self, headers: dict[str, str], identity: str) -> dict[str, str]:
        hdrs = dict(headers or {})
        if identity == "user_b":
            hdrs["User-Agent"] = DEFAULT_CHROME_USER_AGENT
        elif identity == "user_a":
            hdrs["User-Agent"] = self.config.user_agent
        return hdrs

    def quarantine_identity(self, identity: str, duration_sec: float = 65.0) -> None:
        """Quarantines a client identity for at most 65.0 seconds."""
        if identity:
            self._identity_quarantine_until[identity] = time.monotonic() + min(float(duration_sec), 65.0)

    def is_identity_quarantined(self, identity: str) -> bool:
        """Returns True if the client identity is currently quarantined."""
        exp = self._identity_quarantine_until.get(identity)
        if exp is None:
            return False
        if time.monotonic() >= exp:
            self._identity_quarantine_until.pop(identity, None)
            return False
        return True

    def clear_identity_quarantine(self, identity: str) -> None:
        """Clears quarantine status for a client identity."""
        self._identity_quarantine_until.pop(identity, None)

    def _select_scheduled_client(self) -> tuple[Any, str]:
        if self.curl_client_b is None:
            return self.curl_client, "user_a"
        a_q = self.is_identity_quarantined("user_a")
        b_q = self.is_identity_quarantined("user_b")
        if a_q and not b_q:
            self._session_index = 0
            return self.curl_client_b, "user_b"
        if b_q and not a_q:
            self._session_index = 1
            return self.curl_client, "user_a"
        cid, curl = ("user_b", self.curl_client_b) if self._session_index % 2 == 1 else ("user_a", self.curl_client)
        self._session_index += 1
        return curl, cid

    async def _fetch_curl(
        self, fetch_url: str, headers: dict[str, str] | None, start_t: float, client_identity: str | None = None
    ) -> SlotFetchResponse:
        resp = None
        if client_identity == "user_b" and self.curl_client_b is not None:
            curl, cid = self.curl_client_b, "user_b"
            self._session_index = 0
        elif client_identity == "user_a" and self.curl_client is not None:
            curl, cid = self.curl_client, "user_a"
            self._session_index = 1
        else:
            curl, cid = self._select_scheduled_client()
        sanitize = getattr(self, "_sanitize_identity_headers", lambda hdrs, cid: hdrs)
        req_hdrs = sanitize(dict(headers or {}), cid)
        is_xhr = "/web/1/js/items" in fetch_url or "application/json" in req_hdrs.get("Accept", "")
        if is_xhr:
            w_ok = await self._warmup_b_if_needed(curl, req_hdrs.get("Referer") or "https://www.avito.ru/")
            if cid == "user_b" and not w_ok:
                return SlotFetchResponse(
                    status_code=0, url=fetch_url, text="", elapsed_sec=time.perf_counter() - start_t,
                    error="warmup_failed: session_b_not_ready", client_identity=cid,
                )
            req_hdrs.update({
                "Accept": "application/json, text/plain, */*", "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "same-origin", "Referer": req_hdrs.get("Referer", "https://www.avito.ru/"),
                "X-Requested-With": "XMLHttpRequest", "X-Source": "client-browser", "Content-Type": "application/json",
                "Sec-Fetch-User": None, "Upgrade-Insecure-Requests": None,
            })
        actual_wire = {k: v for k, v in h.items()} if isinstance(h := getattr(curl, "headers", None), Mapping) else {}
        actual_wire.update({k: v for k, v in req_hdrs.items() if v is not None})
        for k, v in req_hdrs.items():
            if v is None:
                actual_wire.pop(k, None)
                actual_wire.pop(k.lower(), None)
        try:
            resp = await curl.get(fetch_url, headers=req_hdrs, timeout=self.config.timeout_sec, stream=True)
            if resp.status_code in (403, 429) or resp.status_code == 439:
                self.quarantine_identity(cid, 65.0)
            elif resp.status_code == 200:
                self.clear_identity_quarantine(cid)
            _, text = await read_response_content(resp, self.config.max_body_bytes)
            return SlotFetchResponse(
                status_code=resp.status_code, url=str(resp.url),
                text=text, headers=dict(resp.headers),
                cookies=dict(resp.cookies), elapsed_sec=time.perf_counter() - start_t,
                wire_body_bytes=declared_wire_body_bytes(resp.headers), client_identity=cid,
                request_headers=actual_wire,
            )
        except HttpDeferred:
            raise
        except BodySizeLimitExceededError as exc:
            return SlotFetchResponse(status_code=0, url=fetch_url, text="", elapsed_sec=time.perf_counter() - start_t, error=f"body_size_exceeded: {exc}", client_identity=cid, request_headers=actual_wire)
        except Exception as exc:
            err_str = str(exc).lower()
            err_name = "timeout" if "timeout" in err_str or getattr(exc, "code", None) == 28 else str(exc)
            return SlotFetchResponse(status_code=0, url=fetch_url, text="", elapsed_sec=time.perf_counter() - start_t, error=err_name, client_identity=cid, request_headers=actual_wire)
        finally:
            await safe_teardown_response(resp)

    async def _fetch_httpx(
        self, fetch_url: str, headers: dict[str, str] | None, start_t: float, client_identity: str | None = None
    ) -> SlotFetchResponse:
        assert self.client is not None
        cid = client_identity or "user_a"
        actual_wire = dict(h) if isinstance(h := getattr(self.client, "headers", None), Mapping) else {}
        if headers: actual_wire.update(headers)
        try:
            async with self.client.stream("GET", fetch_url, headers=dict(headers or {})) as resp:
                text = await self._read_stream_body(resp)
                return SlotFetchResponse(
                    status_code=resp.status_code, url=str(resp.url), text=text,
                    headers=dict(resp.headers), cookies=dict(resp.cookies),
                    elapsed_sec=time.perf_counter() - start_t,
                    wire_body_bytes=resp.num_bytes_downloaded if resp.num_bytes_downloaded or not text else None,
                    client_identity=cid, request_headers=actual_wire,
                )
        except UnsafeFetchUrlError:
            return SlotFetchResponse(status_code=0, url="", text="", elapsed_sec=time.perf_counter() - start_t, error="unsafe_fetch_url", client_identity=cid, request_headers=actual_wire)
        except BodySizeLimitExceededError as exc:
            return SlotFetchResponse(status_code=0, url=fetch_url, text="", elapsed_sec=time.perf_counter() - start_t, error=f"body_size_exceeded: {exc}", client_identity=cid, request_headers=actual_wire)
        except httpx.TimeoutException:
            return SlotFetchResponse(status_code=0, url=fetch_url, text="", elapsed_sec=time.perf_counter() - start_t, error="timeout", client_identity=cid, request_headers=actual_wire)
        except httpx.TooManyRedirects:
            return SlotFetchResponse(status_code=0, url=fetch_url, text="", elapsed_sec=time.perf_counter() - start_t, error="too_many_redirects", client_identity=cid, request_headers=actual_wire)
        except httpx.RequestError as exc:
            return SlotFetchResponse(status_code=0, url=fetch_url, text="", elapsed_sec=time.perf_counter() - start_t, error=str(exc), client_identity=cid, request_headers=actual_wire)

    async def fetch(
        self, url: str, headers: dict[str, str] | None = None, client_identity: str | None = None
    ) -> SlotFetchResponse:
        """Fetches URL over persistent HTTP without invoking Chromium or headless browser."""
        fetch_url = url
        if self.config.base_url:
            p = urlparse(url)
            fetch_url = f"{self.config.base_url.rstrip('/')}{p.path}" + (f"?{p.query}" if p.query else "")
        elif not is_allowed_fetch_url(url):
            return SlotFetchResponse(status_code=0, url="", text="", elapsed_sec=0.0, error="unsafe_fetch_url")

        start_t = time.perf_counter()
        if self.curl_client is not None:
            return await self._fetch_curl(fetch_url, headers, start_t, client_identity=client_identity)
        return await self._fetch_httpx(fetch_url, headers, start_t, client_identity=client_identity)

    async def close(self) -> None:
        if self.curl_client is not None: await self.curl_client.close()
        if self.curl_client_b is not None: await self.curl_client_b.close()
        if self.client is not None: await self.client.aclose()


from hunter.transport.http_pool import SlotHttpPool

