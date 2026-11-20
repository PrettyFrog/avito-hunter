"""Transport routing and client isolation for challenge recovery operations (T-969)."""

from __future__ import annotations

import time
from typing import Any
import httpx

from hunter.domain.slots import validate_proxy_endpoint
from hunter.transport.proxy_credentials import ProxyCredentials


def format_proxy_url(endpoint: str | None, credentials: ProxyCredentials | None = None) -> str | None:
    """Formats an HTTP proxy URL string with credentials if present."""
    if not endpoint or endpoint in ("direct", "direct://"):
        return None
    validate_proxy_endpoint(endpoint)
    if not credentials or not credentials.username:
        return endpoint
    prefix, sep, rest = endpoint.partition("://")
    if not sep:
        prefix, rest = "http", endpoint
    user = credentials.username
    pwd = credentials.password or ""
    return f"{prefix}://{user}:{pwd}@{rest}"


def build_recovery_http_client(
    endpoint: str | None,
    headers: dict[str, str] | None = None,
    client: Any = None,
    proxy_credentials: ProxyCredentials | None = None,
    timeout_sec: float = 20.0,
    gateway: Any = None,
    egress_group: str = "default",
) -> tuple[Any, bool]:
    """Provides an HTTP client for challenge resolution.

    If client is passed, returns (client, False) ensuring borrowed clients are never closed.
    If endpoint is a proxy, strictly routes through proxy; direct fallback is forbidden.
    """
    if client is not None:
        return client, False
    proxy_url = format_proxy_url(endpoint, proxy_credentials)
    ua = (headers or {}).get("User-Agent") or "Mozilla/5.0 (X11; Linux x86_64; rv:133.0) Gecko/20100101 Firefox/133.0"
    kwargs = dict(
        proxy=proxy_url,
        timeout=timeout_sec,
        follow_redirects=True,
        headers={"User-Agent": ua},
    )
    if gateway is None:
        c = httpx.AsyncClient(**kwargs)
    else:
        from hunter.transport.attempt_labels import AttemptLabels
        from hunter.transport.http_attempts import observed_httpx_client
        from hunter.transport.metered_http import meter_httpx_client
        c = observed_httpx_client(labels=AttemptLabels("recovery", "unknown", egress_group), **kwargs)
        meter_httpx_client(c, gateway, egress_group=egress_group,
                           generation=0, timeout_sec=timeout_sec)
    return c, True


def _apply_cookie_expiry_to_jar(
    jar: Any, name: str, domain: str, path: str, exp_val: float | None
) -> None:
    if jar is None:
        return
    for c in jar:
        if getattr(c, "name", None) == name:
            c_dom = getattr(c, "domain", None)
            c_path = getattr(c, "path", None)
            dom_match = not domain or not c_dom or domain.lstrip(".") == c_dom.lstrip(".")
            pth_match = not path or not c_path or path == c_path or (path == "/" and c_path in ("/", ""))
            if dom_match and pth_match:
                if exp_val is not None and exp_val > 0:
                    c.expires = int(exp_val)
                    c.discard = False
                elif exp_val == -1:
                    c.expires = None
                    c.discard = True


DISALLOWED_COOKIE_NAMES = frozenset({
    "captcha_v4_user", "bh", "browser_data", "gMltIuegZN2COuSe",
    "cssid", "cssid_exp", "dfp_group", "abp", "csprefid", "test_cookie",
})


def apply_scoped_cookies_to_client(client: Any, cookies: Any, default_domain: str = ".avito.ru") -> None:
    """Applies cookies to client ensuring explicit domain scope and filtering expired cookies."""
    if not cookies or not hasattr(client, "cookies"):
        return
    now_ts = time.time()
    has_adcc = False
    if isinstance(cookies, (list, tuple)):
        has_adcc = any(isinstance(ck, dict) and ck.get("name") == "_adcc" for ck in cookies)
    elif isinstance(cookies, dict):
        has_adcc = "_adcc" in cookies

    if isinstance(cookies, dict):
        for k, v in cookies.items():
            if not k or v is None or k in DISALLOWED_COOKIE_NAMES:
                continue
            if has_adcc and k in ("pow_challenge", "pow_solved"):
                continue
            if hasattr(client.cookies, "set"):
                client.cookies.set(str(k), str(v), domain=default_domain, path="/")
            else:
                client.cookies[str(k)] = str(v)
    elif isinstance(cookies, (list, tuple)):
        for ck in cookies:
            if not (isinstance(ck, dict) and ck.get("name") and ck.get("value") is not None):
                continue
            name = str(ck["name"])
            if name in DISALLOWED_COOKIE_NAMES:
                continue
            if has_adcc:
                if name == "pow_challenge":
                    continue
                if name == "pow_solved" and (ck.get("expires") is None or float(ck.get("expires", 0)) <= now_ts):
                    continue
            exp = ck.get("expires")
            exp_val = None
            if exp is not None:
                try:
                    exp_val = float(exp)
                    if exp_val > 0 and exp_val <= now_ts:
                        continue
                except (ValueError, TypeError):
                    pass
            domain = ck.get("domain") or default_domain
            path = ck.get("path") or "/"
            val = str(ck["value"])
            if hasattr(client.cookies, "set"):
                client.cookies.set(name, val, domain=domain, path=path)
            else:
                client.cookies[name] = val
            jar = getattr(client.cookies, "jar", None)
            _apply_cookie_expiry_to_jar(jar, name, domain, path, exp_val)
            for stored in jar or ():
                if (stored.name, stored.domain, stored.path) == (name, domain, path):
                    stored.secure = bool(ck.get("secure", False))
                    if ck.get("httpOnly"): stored.set_nonstandard_attr("HttpOnly", None)
                    if ck.get("sameSite"): stored.set_nonstandard_attr("SameSite", ck["sameSite"])


def extract_scoped_cookies(client: Any, allowed_domain_suffix: str = "avito.ru") -> list[dict[str, Any]]:
    """Extracts cookies from client jar, strictly isolating allowed domain and preserving path/expires."""
    if not hasattr(client, "cookies"):
        return []
    res: list[dict[str, Any]] = []
    jar = getattr(client.cookies, "jar", None)
    if jar is not None:
        for cookie in jar:
            if str(cookie.name) in DISALLOWED_COOKIE_NAMES:
                continue
            c_domain = getattr(cookie, "domain", "") or ""
            if allowed_domain_suffix and not (
                c_domain in (f".{allowed_domain_suffix}", allowed_domain_suffix)
                or c_domain.endswith(f".{allowed_domain_suffix}")
            ):
                continue
            entry: dict[str, Any] = {
                "name": str(cookie.name),
                "value": str(cookie.value),
                "domain": c_domain or f".{allowed_domain_suffix}",
                "path": getattr(cookie, "path", "/") or "/",
            }
            if getattr(cookie, "expires", None) is not None:
                entry["expires"] = cookie.expires
            res.append(entry)
        return res

    if hasattr(client.cookies, "items"):
        for k, v in client.cookies.items():
            res.append({"name": str(k), "value": str(v), "domain": f".{allowed_domain_suffix}", "path": "/"})
    return res


def merge_scoped_cookies(
    existing: list[dict[str, Any]] | None,
    incoming: Any,
    default_domain: str = ".avito.ru",
) -> list[dict[str, Any]]:
    """Merges cookie lists distinguishing same-name cookies across different domains/paths."""
    from hunter.transport.cookie_delta import CookieDelta
    deleted = getattr(incoming, "deleted", frozenset())
    merged: dict[tuple[str, str, str], dict[str, Any]] = {}

    def _add(c: dict[str, Any]) -> None:
        if not isinstance(c, dict) or not c.get("name") or c.get("value") is None:
            return
        dom = str(c.get("domain") or default_domain)
        pth = str(c.get("path") or "/")
        item: dict[str, Any] = {
            "name": str(c["name"]), "value": str(c["value"]),
            "domain": dom, "path": pth,
        }
        for attr in ("expires", "secure", "httpOnly", "sameSite"):
            if attr in c:
                item[attr] = c[attr]
        merged[(str(c["name"]), dom, pth)] = item

    for c in existing or []:
        _add(c)
    if isinstance(incoming, dict):
        for k, v in incoming.items():
            if k and v is not None:
                _add({"name": str(k), "value": str(v), "domain": default_domain, "path": "/"})
    elif isinstance(incoming, (list, tuple)):
        for c in incoming:
            _add(c)

    for key in deleted:
        merged.pop(key, None)

    if any(c.get("name") == "_adcc" for c in merged.values()):
        now_ts = time.time()
        for k, c in list(merged.items()):
            if k[0] == "pow_challenge":
                del merged[k]
            elif k[0] == "_adcc" and c.get("expires") is None:
                c["expires"] = now_ts + 86400.0

    return CookieDelta(merged.values(), deleted | getattr(existing, "deleted", frozenset()))


def update_cookie_list_from_jar(
    cookies: Any,
    jar: dict[str, str],
    default_domain: str = ".avito.ru",
    expires_map: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """Updates cookie structures with jar values while preserving domain, path, and expiry."""
    res: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    now_ts = time.time()
    exp_map = dict(expires_map or {})
    if "_adcc" in jar and "_adcc" not in exp_map:
        exp_map["_adcc"] = now_ts + 86400.0
    if "pow_solved" in jar and "pow_solved" not in exp_map:
        exp_map["pow_solved"] = now_ts + 10.0

    if isinstance(cookies, list):
        for c in cookies:
            if isinstance(c, dict) and c.get("name"):
                name = str(c["name"])
                seen_names.add(name)
                item = dict(c)
                if name in jar:
                    # Do not overwrite distinct path if another cookie with same name already holds this jar value
                    if any(
                        isinstance(o, dict) and o.get("name") == name and o.get("path") != c.get("path") and o.get("value") == jar[name]
                        for o in cookies
                    ):
                        pass
                    elif c.get("path") in ("/", "") or not any(
                        isinstance(o, dict) and o.get("name") == name and o.get("path") in ("/", "") for o in cookies
                    ):
                        item["value"] = str(jar[name])
                if name in exp_map:
                    item["expires"] = exp_map[name]
                res.append(item)
    elif isinstance(cookies, dict):
        for k, v in cookies.items():
            if k and v is not None:
                name = str(k)
                seen_names.add(name)
                entry = {"name": name, "value": str(jar.get(name, v)), "domain": default_domain, "path": "/"}
                if name in exp_map:
                    entry["expires"] = exp_map[name]
                res.append(entry)
    for k, v in jar.items():
        if k not in seen_names:
            entry = {"name": str(k), "value": str(v), "domain": default_domain, "path": "/"}
            if str(k) in exp_map:
                entry["expires"] = exp_map[str(k)]
            res.append(entry)

    res = [c for c in res if c.get("name") != "pow_challenge"]
    return res
