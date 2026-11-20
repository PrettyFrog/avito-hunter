"""Domain query URL validation, security policy, and canonicalization (S4 / T-473)."""

from __future__ import annotations

import base64
import ipaddress
import json
import re
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse


class QueryUrlError(ValueError): pass
class InvalidSchemeError(QueryUrlError): pass
class UserinfoNotAllowedError(QueryUrlError): pass
class UntrustedHostError(QueryUrlError): pass
class ConflictingPriceError(QueryUrlError): pass

_ALLOWED_HOSTS = {"avito.ru", "www.avito.ru", "m.avito.ru"}
_DISALLOWED_IP_OR_LOCAL = re.compile(
    r"^(localhost|127\.\d+\.\d+\.\d+|0\.0\.0\.0|10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+|172\.(1[6-9]|2\d|3[01])\.\d+\.\d+|::1)$",
    re.I,
)


def is_allowed_fetch_url(url: str) -> bool:
    """Allow only credential-free HTTPS Avito origins at the standard port."""
    try:
        parsed = urlparse(url)
        return (
            parsed.scheme.lower() == "https"
            and parsed.hostname is not None
            and parsed.hostname.lower() in _ALLOWED_HOSTS
            and parsed.port in (None, 443)
            and "@" not in parsed.netloc
            and not parsed.username
            and not parsed.password
        )
    except ValueError:
        return False


def _decode_varint(data: bytes, offset: int = 0) -> tuple[int, int]:
    res, shift, pos = 0, 0, offset
    while pos < len(data):
        b = data[pos]
        pos += 1
        res |= (b & 0x7F) << shift
        if not (b & 0x80):
            return res, pos
        shift += 7
        if shift > 64:
            raise ValueError("Varint too long")
    raise ValueError("Truncated varint")


def _encode_varint(value: int) -> bytes:
    res = bytearray()
    val = value
    while True:
        bits = val & 0x7F
        val >>= 7
        if val:
            res.append(0x80 | bits)
        else:
            res.append(bits)
            break
    return bytes(res)


def extract_f_price_ceiling(f_val: str) -> int | None:
    """Extracts price ceiling 'to' from base64-encoded Avito 'f' parameter if present."""
    try:
        padded = f_val + "=" * ((4 - len(f_val) % 4) % 4)
        raw = base64.urlsafe_b64decode(padded)
        idx = raw.find(b'{"')
        if idx != -1:
            d = json.loads(raw[idx:].decode("utf-8"))
            if isinstance(d, dict) and "to" in d:
                return int(d["to"])
    except Exception:
        pass
    return None


def sanitize_f_param(f_val: str, exact_pmax: int) -> str:
    """Preserves opaque filters while setting 'to' price ceiling and removing price floor."""
    try:
        padded = f_val + "=" * ((4 - len(f_val) % 4) % 4)
        raw = base64.urlsafe_b64decode(padded)
        idx = raw.find(b'{"')
        if idx == -1:
            return f_val

        # Case 1: Plain JSON without protobuf framing
        if idx == 0:
            d = json.loads(raw.decode("utf-8"))
            if not isinstance(d, dict) or ("to" not in d and "from" not in d):
                return f_val
            d["to"] = exact_pmax
            d.pop("from", None)
            new_j = json.dumps(d, separators=(",", ":")).encode("utf-8")
            return base64.urlsafe_b64encode(new_j).decode("ascii").rstrip("=")

        # Case 2: Protobuf framing
        varint_start = idx - 1
        if raw[varint_start] & 0x80 != 0:
            return f_val
        while varint_start > 0 and (raw[varint_start - 1] & 0x80) != 0:
            varint_start -= 1

        old_len, end_pos = _decode_varint(raw, varint_start)
        if end_pos != idx:
            return f_val

        json_bytes = raw[idx:idx + old_len]
        d = json.loads(json_bytes.decode("utf-8"))
        if not isinstance(d, dict) or ("to" not in d and "from" not in d):
            return f_val

        d["to"] = exact_pmax
        d.pop("from", None)
        new_j = json.dumps(d, separators=(",", ":")).encode("utf-8")
        prefix = raw[:varint_start]
        suffix = raw[idx + old_len:]
        new_raw = prefix + _encode_varint(len(new_j)) + new_j + suffix
        return base64.urlsafe_b64encode(new_raw).decode("ascii").rstrip("=")
    except Exception:
        return f_val


def validate_and_canonicalize_query_url(url: str, max_price: int) -> str:
    """Validates and canonicalizes an Avito search URL against strict security and pricing invariants."""
    if not isinstance(max_price, int) or max_price <= 0:
        raise ValueError(f"max_price must be a positive integer, got {max_price}")
    p = urlparse(str(url).strip())
    if p.scheme.lower() != "https":
        raise InvalidSchemeError(f"Only HTTPS URLs are allowed, got '{p.scheme}'")
    if "@" in p.netloc or p.username or p.password:
        raise UserinfoNotAllowedError("URLs with credentials (userinfo) are prohibited")
    h = (p.hostname or "").lower()
    try:
        ipaddress.ip_address(h)
        is_ip_literal = True
    except ValueError:
        is_ip_literal = False
    if p.port not in (None, 443):
        raise UntrustedHostError("Non-standard HTTPS ports are prohibited")
    if is_ip_literal or _DISALLOWED_IP_OR_LOCAL.match(h) or h not in _ALLOWED_HOSTS:
        raise UntrustedHostError(f"Host '{h}' is prohibited")

    qs = parse_qs(p.query, keep_blank_values=False)
    if "pmax" in qs:
        try:
            if int(qs["pmax"][0]) != max_price:
                raise ConflictingPriceError(f"Conflicting pmax: {qs['pmax'][0]} != {max_price}")
        except ValueError:
            raise ConflictingPriceError("Invalid non-integer pmax in query")

    if "f" in qs and qs["f"]:
        f_price = extract_f_price_ceiling(qs["f"][0])
        if f_price is not None and f_price != max_price:
            raise ConflictingPriceError(f"Conflicting price in f parameter: {f_price} != {max_price}")
        qs["f"] = [sanitize_f_param(qs["f"][0], max_price)]

    for k in ("p", "page", "context", "from", "presentationType", "pmin"):
        qs.pop(k, None)
    qs = {k: v for k, v in qs.items() if not k.startswith("utm_")}
    qs["pmax"] = [str(max_price)]
    if "s" not in qs:
        qs["s"] = ["104"]

    netloc = "www.avito.ru" if h == "m.avito.ru" else h
    return urlunparse(("https", netloc, p.path or "/", "", urlencode(qs, doseq=True), ""))
