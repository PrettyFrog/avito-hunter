"""Replay policy for paced anonymous bot traffic (GET/HEAD/POST).

Follow the default libcurl POST redirect rules; paced uploads requiring rewinding
are rejected before wire. Session cookies remain in the owned scoped native jar.
"""
from copy import deepcopy
from urllib.parse import urlsplit

from curl_cffi.requests import Headers


def prepare_paced_request(method: str, options: dict) -> dict:
    method = method.upper()
    if method not in {"GET", "HEAD", "POST"}:
        raise ValueError("Unsupported paced request method")
    if any(options.get(key) for key in ("cookies", "files", "multipart", "discard_cookies")):
        raise ValueError("Paced requests require the owned cookie jar and replayable bodies")
    result = dict(options)
    for key in ("data", "content", "json"):
        value = options.get(key)
        if method != "POST" and value is not None:
            raise ValueError("Paced GET/HEAD request bodies are unsupported")
        if value is not None and not isinstance(value, (str, bytes, bytearray, dict, list, tuple)):
            raise ValueError("Paced body cannot be replayed")
        if key == "content" and value is not None and not isinstance(value, (str, bytes, bytearray)):
            raise ValueError("Paced content cannot be replayed")
        if key in result:
            result[key] = deepcopy(value)
    result["headers"] = Headers(options.get("headers"))
    return result


def _origin(url: str) -> tuple:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None:
        raise ValueError("Unsafe paced redirect target")
    return parsed.scheme, parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == "https" else 80)


def redirect_step(method: str, response, options: dict) -> tuple | None:
    target = response.redirect_url
    if not target or not 300 <= response.status_code < 400:
        return None
    source_origin, target_origin = _origin(response.url), _origin(target)
    result = dict(options)
    headers = Headers(options.get("headers"))
    result["headers"] = headers
    result.pop("params", None)
    if source_origin != target_origin:
        headers["Authorization"] = None
        headers["Cookie"] = None
        result.pop("auth", None)
    if method == "POST" and response.status_code in {301, 302, 303}:
        method = "GET"
        for key in ("data", "content", "json", "files", "multipart"):
            result.pop(key, None)
        for key in ("Content-Length", "Content-Type", "Transfer-Encoding"):
            headers[key] = None
    return method, target, result
