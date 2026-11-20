"""Bounded challenge handoff service with browser queue, PoW solver, and quarantine controls."""

from __future__ import annotations

import asyncio
from contextlib import suppress
import hashlib
import inspect
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any

from loguru import logger

from hunter.collect.firewall_pow import extract_pow_challenge, solve_firewall_pow, solve_firewall_pow_http
from hunter.domain.slots import SlotHealth, SlotSnapshot
from hunter.storage.slot_state import SlotRepository
from hunter.transport.recovery_attempt import RecoveryAttemptReceipt
from hunter.transport.recovery_identity import resolve_session_slot_id, canonical_identity, classify_recovery_exception, RecoveryFailureKind
from hunter.transport.challenge_models import ChallengeOutcome, ChallengeRequest, ChallengeStatus
from hunter.transport.session_store import SessionStore
from hunter.transport.metered_http import HttpDeferred
from hunter.transport.service_singleflight import ServiceSingleFlight


from hunter.transport.challenge_recovery_cookies import (
    is_operator_captcha as _is_operator_captcha,
    extract_page_cookies as _extract_page_cookies,
    merge_cookies as _merge_cookies,
)

BrowserContextFactory = Callable[[SlotSnapshot], Awaitable[Any]]


def should_attempt_challenge(response: Any) -> bool:
    """Determines whether an HTTP response warrants a challenge resolution attempt."""
    if getattr(response, "status_code", None) not in (403, 429):
        return True
    headers = getattr(response, "headers", None) or {}
    if str(headers.get("x-firewall-show-captcha", "")).strip().lower() == "true":
        return True
    text = (getattr(response, "text", "") or "").replace(r"\/", "/")
    if "/firewall/captcha/show" in text or "firewallcaptcha" in text.lower():
        return True
    cookies = getattr(response, "cookies", None)
    token = extract_pow_challenge(text, headers=headers, cookies=cookies)
    if token or _is_operator_captcha("unknown", text):
        return True
    text_lower = text.lower()
    return any(m in text_lower for m in ("pow_challenge", "firewallpow"))


class ChallengeHandoffService:
    """Supervises bounded browser challenge execution and quarantine handoff."""

    def __init__(
        self, slot_repo: SlotRepository, session_store: SessionStore,
        browser_factory: BrowserContextFactory | None = None, max_concurrent_browsers: int = 1,
        default_timeout_sec: float = 90.0, queue_timeout_sec: float = 20.0,
        quarantine_duration_sec: float = 3600.0, http_pow_solver: Any = None,
        allow_browser: bool = True, pow_executor: Any = None,
        egress_gateway: Any = None, egress_groups: Any = None,
    ) -> None:
        self.slot_repo = slot_repo
        self.session_store = session_store
        self.allow_browser = allow_browser
        self.browser_factory = browser_factory if allow_browser else None
        self.http_pow_solver = http_pow_solver
        self.pow_executor = pow_executor
        self.egress_gateway, self.egress_groups = egress_gateway, egress_groups
        self._semaphore = asyncio.Semaphore(max_concurrent_browsers)
        self.default_timeout_sec = default_timeout_sec
        self.queue_timeout_sec = queue_timeout_sec
        self.quarantine_duration_sec = quarantine_duration_sec
        self._slot_locks: dict[str, asyncio.Lock] = {}
        self._recent_refreshes: dict[Any, float] = {}
        self._singleflight = ServiceSingleFlight() if egress_gateway is not None else None
        self._closed = False

    def _refresh_key(self, req: ChallengeRequest):
        identity = canonical_identity(req.client_identity)
        if identity:
            return (req.slot_id, identity)
        return (req.slot_id, "") if self._singleflight else req.slot_id

    def _get_recent_refresh(self, req: ChallengeRequest) -> float:
        identity = canonical_identity(req.client_identity)
        if identity:
            return self._recent_refreshes.get((req.slot_id, identity), 0.0)
        return self._recent_refreshes.get(self._refresh_key(req), self._recent_refreshes.get(req.slot_id, 0.0))

    async def close(self) -> None:
        self._closed = True
        if self._singleflight:
            await self._singleflight.close()

    def _quarantine_timestamp(self, duration_sec: float | None = None) -> str:
        dur = duration_sec if duration_sec is not None else self.quarantine_duration_sec
        return (datetime.now(UTC) + timedelta(seconds=dur)).isoformat()

    async def _quarantine_slot(self, slot_id: str, duration_sec: float | None = None) -> None:
        await self.slot_repo.set_health(
            slot_id=slot_id,
            health=SlotHealth.QUARANTINED,
            quarantined_until=self._quarantine_timestamp(duration_sec),
        )

    async def handle_challenge(self, req: ChallengeRequest) -> ChallengeOutcome:
        """Attach immutable request provenance to every public outcome."""
        if getattr(self, "_closed", False):
            raise RuntimeError("Challenge handoff service is closed")
        identity = canonical_identity(req.client_identity) or ""
        if self._singleflight and identity and type(req.expected_generation) is int and req.expected_generation >= 0:
            outcome = await self._singleflight.run(req.slot_id, identity, req.expected_generation,
                                                   "challenge", lambda: self._handle_owned_challenge(req))
        else:
            outcome = await self._handle_owned_challenge(req)
        failure = outcome.failure_kind
        if failure == RecoveryFailureKind.UNKNOWN:
            failure = RecoveryFailureKind.NONE if outcome.solved else RecoveryFailureKind.RECOVERY_FAILED
        return replace(outcome, client_identity=identity, failure_kind=failure)

    async def _handle_owned_challenge(self, req: ChallengeRequest) -> ChallengeOutcome:
        """Processes an anti-bot challenge for a specific slot within bounded queue."""
        slot = await self.slot_repo.get_slot(req.slot_id)
        if not slot:
            return ChallengeOutcome(
                slot_id=req.slot_id, status=ChallengeStatus.ERROR, solved=False,
                new_session_version=None, quarantined=False, reason=f"Slot '{req.slot_id}' not found",
            )

        slot_lock = self._slot_locks.setdefault(req.slot_id, asyncio.Lock())
        async with slot_lock:
            current_slot = await self.slot_repo.get_slot(req.slot_id)
            if (
                current_slot
                and current_slot.generation == slot.generation
                and (req.expected_generation is None or current_slot.generation == req.expected_generation)
                and current_slot.session_version > slot.session_version
                and current_slot.health == SlotHealth.READY
                and time.monotonic() - self._get_recent_refresh(req) < 15.0
            ):
                return ChallengeOutcome(
                    slot_id=req.slot_id, status=ChallengeStatus.SOLVED, solved=True,
                    new_session_version=current_slot.session_version, quarantined=False,
                    reason="Session refreshed recently by concurrent worker; reusing valid state without browser spawn",
                )
            return await self._process_bounded_challenge(slot, req)

    async def _process_bounded_challenge(self, slot: SlotSnapshot, req: ChallengeRequest) -> ChallengeOutcome:
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=self.queue_timeout_sec)
        except asyncio.TimeoutError:
            await self._quarantine_slot(req.slot_id)
            return ChallengeOutcome(
                slot_id=req.slot_id, status=ChallengeStatus.QUEUE_FULL, solved=False,
                new_session_version=None, quarantined=True,
                reason="Browser queue full; slot quarantined to prevent pool contention",
            )

        timeout = req.timeout_sec or self.default_timeout_sec
        receipt = RecoveryAttemptReceipt(slot_id=req.slot_id)
        try:
            outcome = await asyncio.wait_for(self._execute_challenge(slot, req, receipt=receipt), timeout=timeout)
            if outcome.solved:
                self._recent_refreshes[self._refresh_key(req)] = time.monotonic()
            return outcome
        except asyncio.TimeoutError:
            if receipt.is_committed and receipt.new_version is not None:
                logger.info("Challenge for slot '{}' succeeded before browser teardown timeout", req.slot_id)
                self._recent_refreshes[self._refresh_key(req)] = time.monotonic()
                await self.slot_repo.set_health(req.slot_id, SlotHealth.READY)
                return ChallengeOutcome(
                    slot_id=req.slot_id, status=ChallengeStatus.SOLVED, solved=True,
                    new_session_version=receipt.new_version, quarantined=False,
                    reason="Challenge solved before teardown timeout",
                )
            sq = getattr(req, "allow_quarantine", True)
            if sq: await self._quarantine_slot(req.slot_id, duration_sec=60.0)
            return ChallengeOutcome(
                slot_id=req.slot_id, status=ChallengeStatus.TIMEOUT, solved=False,
                new_session_version=None, quarantined=sq,
                reason=f"Challenge resolution timed out after {timeout:.1f}s",
            )
        except asyncio.CancelledError:
            if getattr(req, "allow_quarantine", True): await self._quarantine_slot(req.slot_id, duration_sec=60.0)
            raise
        except HttpDeferred: raise
        except Exception as exc:
            sq = getattr(req, "allow_quarantine", True)
            if sq: await self._quarantine_slot(req.slot_id, duration_sec=60.0)
            return ChallengeOutcome(
                slot_id=req.slot_id, status=ChallengeStatus.ERROR, solved=False,
                new_session_version=None, quarantined=sq,
                reason=f"Challenge processing crashed: {exc}",
                failure_kind=classify_recovery_exception(exc),
            )
        finally:
            self._semaphore.release()

    async def _save_challenge_state(
        self, slot: SlotSnapshot, page: Any, cookies: list[dict[str, Any]], client_identity: str = "",
        expected_endpoint: str | None = None, expected_generation: int | None = None,
        query_id: str | None = None, assignment_generation: int | None = None,
    ) -> int | None:
        from hunter.storage.recovery_commit import guarded_recovery_commit
        sid = resolve_session_slot_id(slot.slot_id, client_identity)
        old_sess = None
        with suppress(Exception):
            old_sess = await self.session_store.load_session(sid, expected_endpoint=slot.endpoint or "")
        merged = [c for c in _merge_cookies(old_sess.cookies if old_sess else [], cookies) if c.get("name") not in ("f", "ft", "pow_challenge")]
        return await guarded_recovery_commit(
            self.slot_repo, self.session_store, slot, merged, page=page, client_identity=client_identity,
            expected_endpoint=expected_endpoint, expected_generation=expected_generation,
            query_id=query_id, assignment_generation=assignment_generation,
        )

    async def _attempt_http_pow(
        self, slot: SlotSnapshot, req: ChallengeRequest, token: str, receipt: RecoveryAttemptReceipt | None = None
    ) -> ChallengeOutcome | None:
        session_slot_id = resolve_session_slot_id(slot.slot_id, req.client_identity)
        existing_sess = None
        with suppress(Exception):
            existing_sess = await self.session_store.load_session(session_slot_id, expected_endpoint=slot.endpoint or "")
        existing_cookies = existing_sess.cookies if existing_sess else []
        http_cookies = _merge_cookies(existing_cookies, req.cookies)
        solver = self.http_pow_solver or solve_firewall_pow_http
        from hunter.transport.managed_recovery_client import managed_recovery_client
        async with managed_recovery_client(req, req.endpoint or slot.endpoint,
                                           self.egress_gateway, self.egress_groups) as client:
            solved, new_cookies = await solver(
                challenge_token=token, cookies=http_cookies, headers=req.headers,
                endpoint=req.endpoint or slot.endpoint, http_client=client, proxy_credentials=req.proxy_credentials,
                executor=self.pow_executor,
            )
        if not solved:
            return None
        new_version = await self._save_challenge_state(
            slot, None, new_cookies, client_identity=req.client_identity,
            expected_endpoint=req.endpoint or slot.endpoint,
            expected_generation=req.expected_generation if req.expected_generation is not None else slot.generation,
            query_id=req.query_id, assignment_generation=req.assignment_generation,
        )
        if new_version is None:
            return None
        if receipt: receipt.mark_committed(new_version)
        return ChallengeOutcome(
            slot_id=slot.slot_id, status=ChallengeStatus.SOLVED, solved=True,
            new_session_version=new_version, quarantined=False,
            reason="Firewall PoW challenge resolved via HTTP; slot restored to READY",
        )

    async def _attempt_operator_challenge(
        self, slot: SlotSnapshot, req: ChallengeRequest, receipt: RecoveryAttemptReceipt | None, norm_html: str, ip_blocked: bool,
    ) -> ChallengeOutcome:
        failure = RecoveryFailureKind.UNSUPPORTED if req.challenge_type.lower() in ("hcaptcha", "smartcaptcha") else RecoveryFailureKind.RECOVERY_FAILED
        is_disp = str(req.headers.get("x-firewall-show-captcha", "")).lower() == "true" or "/firewall/captcha/show" in norm_html or "firewallcaptcha" in norm_html.lower()
        if is_disp or "geetest" in req.challenge_type.lower() or "geetest" in norm_html.lower():
            try:
                from hunter.transport.geetest_solver import solve_geetest_v4_flow
                from hunter.transport.managed_recovery_client import managed_recovery_client
                async with managed_recovery_client(req, req.endpoint or slot.endpoint,
                                                   self.egress_gateway, self.egress_groups) as client:
                    solved, new_cookies = await solve_geetest_v4_flow(
                        endpoint=req.endpoint or slot.endpoint, headers=req.headers,
                        cookies=req.cookies, html=req.html, client=client,
                        executor=self.pow_executor, fast_path=True,
                    )

                if solved and (new_version := await self._save_challenge_state(
                    slot, None, new_cookies, client_identity=req.client_identity,
                    expected_endpoint=req.endpoint or slot.endpoint,
                    expected_generation=req.expected_generation if req.expected_generation is not None else slot.generation,
                    query_id=req.query_id, assignment_generation=req.assignment_generation,
                )):
                    if receipt: receipt.mark_committed(new_version)
                    return ChallengeOutcome(
                        slot_id=slot.slot_id, status=ChallengeStatus.SOLVED, solved=True,
                        new_session_version=new_version, quarantined=False,
                        reason="GeeTest v4 challenge resolved via offline pure HTTP solver; slot restored to READY",
                    )
            except HttpDeferred:
                raise
            except Exception as exc:
                failure = classify_recovery_exception(exc)
                logger.warning("GeeTest v4 offline solver failed: {}", exc)

        if not (cur_s := await self.slot_repo.get_slot(slot.slot_id)) or cur_s.health == SlotHealth.DISABLED:
            return ChallengeOutcome(slot.slot_id, ChallengeStatus.ERROR, False, None, False, reason="Slot disabled")
        sq = getattr(req, "allow_quarantine", True)
        if sq: await self._quarantine_slot(slot.slot_id, duration_sec=62.0 if ip_blocked else None)
        return ChallengeOutcome(
            slot_id=slot.slot_id, status=ChallengeStatus.OPERATOR_REQUIRED, solved=False,
            new_session_version=None, quarantined=sq,
            reason="Unresolved hCaptcha/GeeTest challenge; operator intervention required",
            failure_kind=failure,
        )

    async def _execute_challenge(
        self, slot: SlotSnapshot, req: ChallengeRequest, receipt: RecoveryAttemptReceipt | None = None
    ) -> ChallengeOutcome:
        """Executes HTTP PoW challenge solver, browser fallback, or flags GeeTest as operator required."""
        norm_html = req.html.replace(r"\/", "/")
        ip_blocked = any(m in norm_html.lower() for m in ("проблема с ip", "доступ ограничен", "временно заблокирован", "too-many-requests"))
        cooldown_dur = 62.0 if ip_blocked else 60.0

        is_disp = str(req.headers.get("x-firewall-show-captcha", "")).lower() == "true" or "/firewall/captcha/show" in norm_html or "firewallcaptcha" in norm_html.lower()
        if _is_operator_captcha(req.challenge_type, req.html) or is_disp:
            return await self._attempt_operator_challenge(slot, req, receipt, norm_html, ip_blocked)

        token = req.challenge_token or extract_pow_challenge(req.html, headers=req.headers, cookies=req.cookies)
        if token:
            if (http_outcome := await self._attempt_http_pow(slot, req, token, receipt=receipt)) is not None:
                return http_outcome
            if not (cur_s := await self.slot_repo.get_slot(slot.slot_id)) or cur_s.health == SlotHealth.DISABLED:
                return ChallengeOutcome(slot.slot_id, ChallengeStatus.ERROR, False, None, False, reason="Slot disabled")

        sq = getattr(req, "allow_quarantine", True)
        if not token and req.html:
            if sq: await self._quarantine_slot(slot.slot_id, duration_sec=cooldown_dur)
            reason = "IP rate limit cooldown (62s)" if ip_blocked else "No solvable PoW challenge token found in response; slot quarantined without browser invocation"
            return ChallengeOutcome(slot.slot_id, ChallengeStatus.ERROR, False, None, sq, reason=reason)

        if not self.browser_factory or not self.allow_browser:
            if sq: await self._quarantine_slot(slot.slot_id, duration_sec=cooldown_dur)
            return ChallengeOutcome(slot.slot_id, ChallengeStatus.ERROR, False, None, sq, reason="No browser factory configured or browser execution disabled")

        return await self._execute_browser_pow(slot, req, token, receipt=receipt)

    async def _execute_browser_pow(
        self, slot: SlotSnapshot, req: ChallengeRequest, token: str | None, receipt: RecoveryAttemptReceipt | None = None
    ) -> ChallengeOutcome:
        page = None
        try:
            page = await self.browser_factory(slot)
            solved = await solve_firewall_pow(page, html=req.html, challenge_token=token)
            cookies = await _extract_page_cookies(page)
            if not solved and "pow_solved" in {c.get("name") for c in cookies if isinstance(c, dict)}:
                solved = True

            if not solved:
                await self._quarantine_slot(slot.slot_id, duration_sec=60.0)
                return ChallengeOutcome(
                    slot_id=slot.slot_id, status=ChallengeStatus.OPERATOR_REQUIRED, solved=False,
                    new_session_version=None, quarantined=True,
                    reason="Firewall PoW solver failed; operator intervention required",
                )

            new_version = await self._save_challenge_state(
                slot, page, cookies, client_identity=req.client_identity,
                expected_endpoint=req.endpoint or slot.endpoint,
                expected_generation=req.expected_generation if req.expected_generation is not None else slot.generation,
                query_id=req.query_id, assignment_generation=req.assignment_generation,
            )
            if new_version is None:
                return ChallengeOutcome(
                    slot_id=slot.slot_id, status=ChallengeStatus.ERROR, solved=False,
                    new_session_version=None, quarantined=False, reason="Guarded commit rejected: slot disabled or reassigned",
                )
            if receipt: receipt.mark_committed(new_version)
            return ChallengeOutcome(
                slot_id=slot.slot_id, status=ChallengeStatus.SOLVED, solved=True,
                new_session_version=new_version, quarantined=False,
                reason="Firewall PoW challenge resolved and validated; slot restored to READY",
            )
        finally:
            if page and hasattr(page, "close"):
                if inspect.iscoroutinefunction(page.close):
                    await page.close()
                else:
                    page.close()
