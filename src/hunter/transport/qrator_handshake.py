"""Isolated Qrator WAF browserless handshake service (Phase 1 / T-480)."""
from __future__ import annotations

import inspect
import json
import secrets
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from loguru import logger

from hunter.transport.attempt_scope import scoped_http_call
from hunter.transport.metered_http import HttpDeferred
from hunter.transport.qrator_codec import DEFAULT_BASE_S, build_qrator_multipart, encode_field, generate_qrator_f
from hunter.transport.service_singleflight import ServiceSingleFlight


class HandshakeOutcome(str, Enum):
    SUCCESS = "success"
    BLOCKED = "blocked"
    RATE_LIMITED = "rate_limited"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class QratorHandshakeResult:
    outcome: HandshakeOutcome
    is_success: bool
    f_val: str | None = None
    ft_val: str | None = None
    cookies: list[dict[str, Any]] = field(default_factory=list)
    retry_after_sec: float | None = None
    acquired_at: float | None = None
    details: str = ""


def is_ft_expired(acquired_at: float | None, assumed_ttl_sec: float = 64800.0) -> bool:
    """Checks whether token age exceeds the assumed TTL threshold."""
    if acquired_at is None or acquired_at <= 0:
        return True
    return (time.time() - acquired_at) >= assumed_ttl_sec


def _parse_retry_after(headers: Any) -> float | None:
    if not headers or not hasattr(headers, "get"):
        return None
    val = headers.get("retry-after") or headers.get("Retry-After")
    if not val:
        return None
    from hunter.domain.recovery_policy import parse_retry_after
    return parse_retry_after(val)


def _extract_ft_token(body_text: str) -> str | None:
    cleaned = body_text.strip()
    if not cleaned:
        return None
    if cleaned.startswith('"') and cleaned.endswith('"'):
        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, str) and len(parsed) >= 32 and not parsed.startswith("{") and " " not in parsed:
                return parsed
        except Exception:
            pass
    if cleaned.startswith("{") and cleaned.endswith("}"):
        try:
            data = json.loads(cleaned)
            if isinstance(data, dict):
                cand = data.get("ft") or data.get("token") or data.get("result")
                if isinstance(cand, str) and len(cand) >= 32 and not cand.startswith("{") and " " not in cand:
                    return cand
        except Exception:
            pass
        return None
    if len(cleaned) >= 32 and not cleaned.startswith("<") and not cleaned.startswith("{") and " " not in cleaned:
        return cleaned
    return None


def _build_cookies(f_val: str, ft_val: str) -> list[dict[str, Any]]:
    return [
        {"name": "f", "value": f_val, "domain": ".avito.ru", "path": "/"},
        {"name": "ft", "value": ft_val, "domain": ".avito.ru", "path": "/"},
    ]


async def _apply_cookies_to_client(client: Any, cookies: list[dict[str, Any]]) -> None:
    if not hasattr(client, "cookies"):
        return
    c_obj = client.cookies
    for c in cookies:
        if hasattr(c_obj, "set"):
            res = c_obj.set(c["name"], c["value"], domain=c.get("domain", ".avito.ru"), path=c.get("path", "/"))
            if inspect.isawaitable(res):
                await res
        elif isinstance(c_obj, dict):
            c_obj[c["name"]] = c["value"]


async def _send_ft_request(client: Any, purpose: str, f_val: str, timeout_sec: float) -> Any:
    ct, body = build_qrator_multipart(f_val, encode_field(DEFAULT_BASE_S, 's'))
    headers = {
        "Content-Type": ct,
        "Accept": "*/*",
        "Origin": "https://www.avito.ru",
        "Referer": "https://www.avito.ru/",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }
    return await scoped_http_call(
        client, purpose, "post",
        "https://www.avito.ru/web/2/ft",
        headers=headers,
        data=body,
        timeout=timeout_sec,
    )


async def _ping_qrator_pixel(client: Any, purpose: str) -> tuple[HandshakeOutcome, float | None, str] | None:
    try:
        rnd = secrets.randbelow(1_000_000_000)
        pixel_headers = {
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            "Referer": "https://www.avito.ru/",
            "Sec-Fetch-Dest": "image",
            "Sec-Fetch-Mode": "no-cors",
            "Sec-Fetch-Site": "same-origin",
        }
        resp = await scoped_http_call(
            client, purpose, "get",
            f"https://www.avito.ru/web/1/u?{rnd}",
            headers=pixel_headers,
            timeout=5.0,
        )
        st = getattr(resp, "status_code", 0)
        hdrs = getattr(resp, "headers", {}) or {}
        if st == 429:
            return HandshakeOutcome.RATE_LIMITED, _parse_retry_after(hdrs), "Qrator pixel HTTP 429 Rate Limited"
        if st == 403:
            return HandshakeOutcome.BLOCKED, None, "Qrator pixel HTTP 403 Forbidden"
        if not 200 <= st < 300:
            return HandshakeOutcome.ERROR, None, f"Qrator pixel HTTP {st}"
        return None
    except HttpDeferred:
        raise
    except Exception as exc:
        logger.debug("Qrator pixel ping failed: {}", exc)
        return HandshakeOutcome.ERROR, None, "Qrator pixel request failed"


def _evaluate_ft_response(resp: Any) -> tuple[HandshakeOutcome, str | None, float | None, str]:
    status = getattr(resp, "status_code", 0)
    headers = getattr(resp, "headers", {}) or {}
    if status == 429:
        return HandshakeOutcome.RATE_LIMITED, None, _parse_retry_after(headers), "HTTP 429 Rate Limited"
    if status == 403:
        return HandshakeOutcome.BLOCKED, None, None, "HTTP 403 Forbidden"
    if status != 200:
        return HandshakeOutcome.ERROR, None, None, f"HTTP {status}"
    text = getattr(resp, "text", "") or ""
    ft_token = _extract_ft_token(text)
    if not ft_token:
        return HandshakeOutcome.ERROR, None, None, "Invalid ft token response"
    return HandshakeOutcome.SUCCESS, ft_token, None, ""


async def _execute_staged_handshake(
    client: Any,
    f_val: str,
    purpose: str = "warmup",
    timeout_sec: float = 10.0,
) -> QratorHandshakeResult:
    try:
        await _apply_cookies_to_client(client, [{"name": "f", "value": f_val, "domain": ".avito.ru", "path": "/"}])
        resp = await _send_ft_request(client, purpose, f_val, timeout_sec)
        outcome, ft_token, retry_after, details = _evaluate_ft_response(resp)
        if outcome != HandshakeOutcome.SUCCESS or not ft_token:
            return QratorHandshakeResult(
                outcome=outcome, is_success=False, retry_after_sec=retry_after, details=details,
            )

        await _apply_cookies_to_client(client, [{"name": "ft", "value": f'"{ft_token}"', "domain": ".avito.ru", "path": "/"}])
        pix_err = await _ping_qrator_pixel(client, purpose)
        if pix_err is not None:
            pix_outcome, pix_ra, pix_details = pix_err
            return QratorHandshakeResult(
                outcome=pix_outcome, is_success=False, retry_after_sec=pix_ra, details=pix_details,
            )

        cookies = _build_cookies(f_val, ft_token)
        return QratorHandshakeResult(
            outcome=HandshakeOutcome.SUCCESS,
            is_success=True,
            f_val=f_val,
            ft_val=ft_token,
            cookies=cookies,
            acquired_at=time.time(),
            details="Qrator handshake completed successfully",
        )
    except HttpDeferred:
        raise
    except Exception as exc:
        logger.warning("Qrator handshake request failed: {}", exc)
        return QratorHandshakeResult(outcome=HandshakeOutcome.ERROR, is_success=False, details=str(exc))


async def _execute_handshake_flow(client: Any, purpose: str = "warmup", timeout_sec: float = 10.0) -> QratorHandshakeResult:
    from hunter.transport.qrator_staging import StagedHandshakeClient, profile_cookie_snapshot
    ch = getattr(client, "_qrator_canvas_hash", None)
    if not isinstance(ch, int):
        ch = 4 + secrets.randbelow(0xFFFFFFFA)
        setattr(client, "_qrator_canvas_hash", ch)
    prev = profile_cookie_snapshot(client)
    stage = StagedHandshakeClient(client)
    try:
        res = await _execute_staged_handshake(stage, generate_qrator_f(canvas_hash=ch), purpose, timeout_sec)
        if res.is_success:
            if prev is not None and profile_cookie_snapshot(client) != prev:
                return QratorHandshakeResult(HandshakeOutcome.ERROR, False, details="Profile changed during handshake")
            from hunter.transport.cookie_delta import commit_staged_cookies
            from dataclasses import replace
            res = replace(res, cookies=commit_staged_cookies(client, stage))
        return res
    finally:
        await stage.close()


async def perform_qrator_handshake(
    client: Any,
    slot_id: str,
    identity: str = "user_a",
    generation: int = 0,
    purpose: str = "warmup",
    timeout_sec: float = 10.0,
    single_flight: ServiceSingleFlight | None = None,
) -> QratorHandshakeResult:
    """Coordinates a safe single-flight Qrator handshake for the slot identity."""
    if single_flight is not None:
        return await single_flight.run(
            slot_id, identity, generation, "qrator_handshake",
            lambda: _execute_handshake_flow(client, purpose=purpose, timeout_sec=timeout_sec),
        )
    return await _execute_handshake_flow(client, purpose=purpose, timeout_sec=timeout_sec)


async def enrich_recovery_cookies(
    client: Any,
    slot_id: str,
    identity: str = "user_a",
    base_cookies: list[dict[str, Any]] | None = None,
    generation: int = 0,
) -> list[dict[str, Any]]:
    """Non-blocking enrichment of recovery cookies with Qrator f/ft tokens."""
    cookies = list(base_cookies or [])
    try:
        cid = "user_b" if identity == "user_b" else "user_a"
        hs = await perform_qrator_handshake(
            client=client,
            slot_id=slot_id,
            identity=cid,
            generation=generation,
            purpose="recovery",
            timeout_sec=10.0,
        )
        if hs.is_success and hs.cookies:
            from hunter.transport.recovery_transport import merge_scoped_cookies
            return merge_scoped_cookies(cookies, hs.cookies)
        if hs.outcome == HandshakeOutcome.RATE_LIMITED and hs.retry_after_sec:
            logger.info("Qrator handshake rate limited on recovery; cooldown {}s", hs.retry_after_sec)
    except Exception as exc:
        logger.debug("Non-critical handshake failure during recovery: {}", exc)
    return cookies

