"""Typed response classification and health signals for S5 slots (T-480).

Contracts:
- Typed outcomes: OK, EXPLICIT_EMPTY, UNKNOWN, BLOCKED, CHALLENGE, AUTH_REQUIRED.
- MFE parser reuse (parse_mfe_state, extract_mfe_state_json).
- SDK captcha mention + feed present = OK (not challenge).
- Empty marker = EXPLICIT_EMPTY.
- UNKNOWN does not clear seen.
- Cookie presence + HTTP 200 does not equal auth healthy (inspects user session in MFE).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from hunter.collect.parsers.mfe_state import (
    extract_mfe_state_json,
    parse_mfe_state,
)
from hunter.collect.parsers.search import parse_search
from hunter.core.models import SearchHit
from hunter.domain.slots import SlotSignal


class ResponseOutcome(str, Enum):
    OK = "ok"
    EXPLICIT_EMPTY = "explicit_empty"
    UNKNOWN = "unknown"
    BLOCKED = "blocked"
    CHALLENGE = "challenge"
    AUTH_REQUIRED = "auth_required"


CHALLENGE_MARKERS = (
    "firewall/captcha",
    "smartcaptcha.yandexcloud.net",
    "captcha-wrapper",
    "data-marker=\"captcha-box\"",
    "geetest_holder",
    "Доступ ограничен: проблема с IP",
    "проверка безопасности",
    "firewall__title",
    "pow_challenge",
)

BLOCKED_MARKERS = (
    "Доступ с вашего IP временно ограничен",
    "IP-адрес временно заблокирован",
    "Доступ запрещен",
    "blocked by cloudflare",
    "security-firewall-block",
)

# IP/network-level markers only. Do not add generic "Доступ ограничен",
# "Доступ запрещен" or "проверка безопасности": captcha/WAF pages use them.
IP_BLOCK_MARKERS = (
    "проблема с ip",
    "проблем с ip",
    "ip-адрес временно заблокирован",
    "доступ с вашего ip временно ограничен",
    "blocked by cloudflare",
    "security-firewall-block",
)

AUTH_REQUIRED_MARKERS = (
    "/profile/login",
    "passport.avito.ru",
    "auth/login",
    "Войдите, чтобы продолжить",
    "Войти или зарегистрироваться",
)

AUTH_GATE_MARKERS = (
    "Войдите, чтобы продолжить",
    "Войти или зарегистрироваться",
    "data-marker=\"auth-gate\"",
)


@dataclass(frozen=True, slots=True)
class ClassificationResult:
    outcome: ResponseOutcome
    status_code: int
    hits: list[SearchHit] = field(default_factory=list)
    details: str = ""
    is_authenticated: bool | None = None

    @property
    def is_healthy(self) -> bool:
        return self.outcome in (ResponseOutcome.OK, ResponseOutcome.EXPLICIT_EMPTY)

    @property
    def should_update_seen(self) -> bool:
        """Only genuine OK and explicitly empty responses update seen; UNKNOWN never does."""
        return self.outcome in (ResponseOutcome.OK, ResponseOutcome.EXPLICIT_EMPTY)

    @property
    def suggested_signal(self) -> SlotSignal | None:
        if self.outcome in (ResponseOutcome.OK, ResponseOutcome.EXPLICIT_EMPTY):
            return SlotSignal.PROBE_OK
        if self.outcome == ResponseOutcome.CHALLENGE:
            return SlotSignal.CHALLENGE_DETECTED
        if self.outcome == ResponseOutcome.BLOCKED:
            return SlotSignal.HTTP_429 if self.status_code in (429, 439) else SlotSignal.HTTP_403
        if self.outcome == ResponseOutcome.AUTH_REQUIRED:
            return SlotSignal.AUTH_INVALID
        return None


def _check_user_auth(mfe_json: dict[str, Any] | None) -> bool | None:
    if not mfe_json or not isinstance(mfe_json, dict):
        return None
    user = mfe_json.get("user") or mfe_json.get("auth")
    if not isinstance(user, dict):
        loader = mfe_json.get("loaderData", {})
        if isinstance(loader, dict):
            user = loader.get("user") or loader.get("auth")
    if isinstance(user, dict):
        if "isAuth" in user:
            return bool(user["isAuth"])
        if "isAuthorized" in user:
            return bool(user["isAuthorized"])
        if user.get("id") or user.get("userId"):
            return True
    return None


def _is_empty_json(obj: dict[str, Any]) -> bool:
    if not isinstance(obj, dict):
        return False
    if obj.get("status") in ("error", "fail", "failed") or obj.get("error") or obj.get("errors"):
        return False
    cnt = obj.get("totalCount") if obj.get("totalCount") is not None else obj.get("count")
    try:
        numeric_cnt = int(cnt) if cnt is not None else None
    except (ValueError, TypeError):
        numeric_cnt = None
    if numeric_cnt is not None and numeric_cnt > 0:
        return False
    catalog = obj.get("catalog")
    items = catalog.get("items") if isinstance(catalog, dict) else obj.get("items")
    if numeric_cnt == 0:
        return items is None or (isinstance(items, list) and len(items) == 0)
    return isinstance(items, list) and len(items) == 0


def _is_empty_mfe(mfe_json: dict[str, Any]) -> bool:
    if not isinstance(mfe_json, dict) or mfe_json.get("status") in ("error", "fail", "failed"):
        return False
    loader = mfe_json.get("loaderData", {})
    data = loader.get("data", {}) if isinstance(loader, dict) else {}
    if not isinstance(data, dict) or data.get("status") in ("error", "fail", "failed"):
        return False
    cnt = data.get("count") if data.get("count") is not None else data.get("totalCount")
    try:
        numeric_cnt = int(cnt) if cnt is not None else None
    except (ValueError, TypeError):
        numeric_cnt = None
    if numeric_cnt is not None and numeric_cnt > 0:
        return False
    catalog = data.get("catalog", {}) if isinstance(data.get("catalog"), dict) else {}
    items = catalog.get("items") if isinstance(catalog, dict) else data.get("items")
    if numeric_cnt == 0:
        return items is None or (isinstance(items, list) and len(items) == 0)
    return isinstance(items, list) and len(items) == 0


def _check_empty_markers(html: str, mfe_json: dict[str, Any] | None) -> bool:
    if mfe_json and _is_empty_mfe(mfe_json):
        return True
    stripped = html.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            import json
            obj = json.loads(stripped)
            return _is_empty_json(obj)
        except Exception:
            return False
    lower = html.lower()
    return (
        "data-marker=\"catalog-empty\"" in html
        or "ничего не найдено" in lower
        or "объявлений пока нет" in lower
        or "похожие объявления рядом" in lower
    )


def _classify_empty_or_unknown(
    status_code: int, html: str, require_auth: bool
) -> ClassificationResult:
    html_lower = html.lower()
    if status_code == 439 or any(marker.lower() in html_lower for marker in CHALLENGE_MARKERS):
        return ClassificationResult(
            outcome=ResponseOutcome.CHALLENGE,
            status_code=status_code,
            details="Active challenge barrier detected",
        )
    if any(marker in html for marker in BLOCKED_MARKERS):
        return ClassificationResult(
            outcome=ResponseOutcome.BLOCKED,
            status_code=status_code,
            details="WAF or IP block page detected",
        )
    mfe_json = extract_mfe_state_json(html)
    is_auth = _check_user_auth(mfe_json)
    if _check_empty_markers(html, mfe_json):
        if require_auth and is_auth is False:
            return ClassificationResult(
                outcome=ResponseOutcome.AUTH_REQUIRED,
                status_code=status_code,
                details="Empty catalog but session is unauthenticated",
                is_authenticated=False,
            )
        return ClassificationResult(
            outcome=ResponseOutcome.EXPLICIT_EMPTY,
            status_code=status_code,
            details="Explicit empty catalog confirmed",
            is_authenticated=is_auth,
        )
    if (require_auth and is_auth is False) or any(m in html for m in AUTH_GATE_MARKERS):
        return ClassificationResult(
            outcome=ResponseOutcome.AUTH_REQUIRED,
            status_code=status_code,
            details="Authentication required or explicit auth gate",
            is_authenticated=False,
        )
    if require_auth and any(m in html for m in AUTH_REQUIRED_MARKERS):
        return ClassificationResult(
            outcome=ResponseOutcome.AUTH_REQUIRED,
            status_code=status_code,
            details="Authentication required or session expired",
            is_authenticated=False,
        )
    return ClassificationResult(
        outcome=ResponseOutcome.UNKNOWN,
        status_code=status_code,
        details="Unrecognized HTML structure or API error; must not clear seen",
    )


def classify_response(
    status_code: int,
    html: str,
    url: str = "",
    require_auth: bool = False,
) -> ClassificationResult:
    """Classifies an HTTP response into a typed outcome with health signals."""
    if status_code in (403, 429, 439):
        html_lower = html.replace(r"\/", "/").lower()
        if status_code == 439 or any(marker.lower() in html_lower for marker in CHALLENGE_MARKERS):
            return ClassificationResult(
                outcome=ResponseOutcome.CHALLENGE, status_code=status_code, details="Active challenge barrier detected",
            )
        return ClassificationResult(
            outcome=ResponseOutcome.BLOCKED, status_code=status_code, details=f"HTTP {status_code}",
        )
    if any(m in url for m in ("/profile/login", "passport.avito.ru")):
        return ClassificationResult(
            outcome=ResponseOutcome.AUTH_REQUIRED, status_code=status_code, details="Redirected to login/passport endpoint",
        )
    if status_code != 200:
        is_blk = 400 <= status_code < 600 and any(m in html for m in BLOCKED_MARKERS)
        return ClassificationResult(
            outcome=ResponseOutcome.BLOCKED if is_blk else ResponseOutcome.UNKNOWN,
            status_code=status_code,
            details=f"HTTP {status_code} non-200 response; not eligible for feed or empty outcome",
        )

    hits = parse_mfe_state(html)
    if not hits:
        stripped = html.strip()
        if (stripped.startswith("{") and stripped.endswith("}")) or "data-marker=\"catalog-serp\"" in html or "data-marker=\"item\"" in html:
            hits = parse_search(html)

    # Contract: SDK captcha mention + feed = OK
    if hits:
        mfe_json = extract_mfe_state_json(html)
        is_auth = _check_user_auth(mfe_json)
        if require_auth and is_auth is False:
            return ClassificationResult(
                outcome=ResponseOutcome.AUTH_REQUIRED,
                status_code=status_code,
                hits=hits,
                details="Feed present but user session is unauthenticated (HTTP 200 with cookies != healthy auth)",
                is_authenticated=False,
            )
        return ClassificationResult(
            outcome=ResponseOutcome.OK,
            status_code=status_code,
            hits=hits,
            details=f"Catalog feed parsed successfully ({len(hits)} hits)",
            is_authenticated=is_auth,
        )

    return _classify_empty_or_unknown(status_code, html, require_auth)


def is_ip_level_block(response: Any) -> bool:
    """Checks whether the response indicates a hard IP-level firewall block."""
    if not response:
        return False
    status = getattr(response, "status_code", None)
    if status not in (403, 429):
        return False
    text = (getattr(response, "text", "") or "").lower()
    text = re.sub(r"\s+", " ", text)
    return any(marker.lower() in text for marker in IP_BLOCK_MARKERS)
