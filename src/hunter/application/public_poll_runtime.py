"""Public HTTP-to-observation polling runtime for persisted slot assignments."""

from __future__ import annotations

import asyncio
from contextlib import suppress
import inspect
import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from hunter.application.metrics import EventLatencyProvenance, LowCardinalityMetricsCollector
from hunter.application.session_cleanup import capture_origin_profile, cleanup_after_outcome, reload_recovered_profile, outcome_matches_request
from hunter.transport.recovery_identity import canonical_identity, peer_identity
from hunter.application.poll_attempts import (
    _parse_outcome,
    _record_poll_metrics,
    assemble_poll_batch,
    can_fallback_to_html,
    classify_retry_signal,
    execute_challenge_with_peer_failover,
    extract_retry_after,
    has_peer_identity,
    safe_fetch_attempt,
)
from hunter.application.slot_worker import SlotWorker
from hunter.collect.parsers.search import parse_search
from hunter.domain.observations import ObservationBatch, ObservedListing, ParseOutcome, PriceState
from hunter.domain.poll_policy import PollPolicy
from hunter.domain.slots import SlotSignal
from hunter.transport.catalog_api import invalidate_ambiguous_poll_target, invalidate_poll_target, record_poll_response, resolve_poll_target, to_wire_poll_url
from hunter.transport.challenge import should_attempt_challenge
from hunter.transport.classification import ResponseOutcome, classify_response
from hunter.transport.http_fetch import SlotHttpPool
from hunter.transport.metered_http import HttpDeferred
from loguru import logger

@dataclass(frozen=True, slots=True)
class QueryAssignment:
    slot_id: str
    endpoint: str
    query_id: str
    canonical_url: str
    generation: int
    position: int
    slot_generation: int = 0
    request_headers: dict[str, str] = field(default_factory=dict)


async def load_active_assignments(conn_mgr: Any) -> list[QueryAssignment]:
    reader = await conn_mgr.open_read_connection()
    try:
        rows = await (await reader.execute(
            "SELECT a.slot_id, s.endpoint, a.query_id, q.canonical_url, a.generation, a.position, s.health_state, s.active_generation "
            "FROM assignments a JOIN slots s ON s.id = a.slot_id JOIN queries q ON q.id = a.query_id "
            "WHERE a.active = 1 ORDER BY a.slot_id, a.position"
        )).fetchall()
        excluded = {"quarantined", "auth_required", "challenge"}
        return [
            QueryAssignment(
                str(row[0]), str(row[1]), str(row[2]), str(row[3]), int(row[4]), int(row[5]),
                slot_generation=int(row[7]) if len(row) > 7 and row[7] is not None else int(row[4]),
            )
            for row in rows if str(row[6]).lower() not in excluded
        ]
    finally:
        await conn_mgr.close_reader(reader)


async def _assignment_poll_state(conn_mgr: Any, assignment: QueryAssignment) -> str:
    reader = await conn_mgr.open_read_connection()
    try:
        cur = await reader.execute(
            "SELECT a.generation, a.active, lower(s.health_state) FROM assignments a "
            "JOIN slots s ON s.id = a.slot_id WHERE a.slot_id = ? AND a.query_id = ?",
            (assignment.slot_id, assignment.query_id),
        )
        row = await cur.fetchone()
        if row is None or not bool(row[1]): return "stale"
        if str(row[2]) in {"disabled", "quarantined", "auth_required", "challenge"}: return "disabled"
        sub_c = await reader.execute(
            "SELECT 1 FROM subscriptions s "
            "JOIN users u ON u.id = s.user_id "
            "WHERE s.query_id = ? AND s.enabled = 1 AND u.status = 'active' LIMIT 1",
            (assignment.query_id,),
        )
        if await sub_c.fetchone() is None: return "paused"
        return "current" if int(row[0]) == assignment.generation else "stale"
    finally:
        await conn_mgr.close_reader(reader)


class PublicPollRuntime:
    """Runs one sequential SlotWorker per slot and fences responses by assignment generation."""

    def __init__(
        self,
        conn_mgr: Any,
        http_pool: SlotHttpPool,
        ingest: Any,
        policy: PollPolicy,
        *,
        is_paused: Any = None,
        next_sequence: Any = None,
        slot_signal_handler: Any = None,
        metrics: LowCardinalityMetricsCollector | None = None,
        challenge_service: Any = None,
        require_auth: bool = False,
        identity_cleanup: bool | None = None,
    ) -> None:
        self.conn_mgr = conn_mgr
        self.http_pool = http_pool
        self.ingest = ingest
        self.policy = policy
        self.is_paused = is_paused
        self.next_sequence = next_sequence
        self.slot_signal_handler = slot_signal_handler
        self.metrics = metrics or LowCardinalityMetricsCollector()
        self.challenge_service = challenge_service
        self.require_auth = require_auth
        self.identity_cleanup = identity_cleanup if identity_cleanup is not None else os.environ.get("HUNTER_IDENTITY_CLEANUP", "0").lower() in ("1", "true", "yes")
        self.workers: dict[str, SlotWorker] = {}
        self.retiring_workers: set[SlotWorker] = set()
        self.assignments: dict[tuple[str, str], QueryAssignment] = {}
        self._refresh_lock = asyncio.Lock()
        self._is_stopped = False

    async def _report_slot_failure(
        self, assignment: QueryAssignment, signal: SlotSignal, quarantine_duration_sec: float | None = None
    ) -> None:
        if self.slot_signal_handler is not None:
            kwargs: dict[str, Any] = {
                "query_id": assignment.query_id,
                "assignment_generation": assignment.generation,
            }
            if quarantine_duration_sec is not None:
                kwargs["quarantine_duration_sec"] = quarantine_duration_sec
            await self.slot_signal_handler(
                assignment.slot_id, signal, **kwargs
            )

    async def _handle_challenge(
        self, slot_id_or_assignment: str | QueryAssignment, response: Any,
        allow_quarantine: bool = True, client_identity: str | None = None,
    ) -> Any:
        try:
            from hunter.application.poll_attempts import prepare_challenge_request
            from hunter.transport.challenge import ChallengeStatus

            slot_id = slot_id_or_assignment if isinstance(slot_id_or_assignment, str) else slot_id_or_assignment.slot_id
            req = await prepare_challenge_request(
                slot_id_or_assignment, response, self.http_pool, self.challenge_service,
                allow_quarantine=allow_quarantine, client_identity=client_identity,
            )
            if self.identity_cleanup and canonical_identity(req.client_identity) is None:
                return None
            origin_fence = await capture_origin_profile(self, req) if self.identity_cleanup else None
            outcome = await self.challenge_service.handle_challenge(req)
            if self.identity_cleanup and not outcome_matches_request(req, outcome):
                return None
            await cleanup_after_outcome(self, req, outcome, origin_fence)
            if getattr(outcome, "solved", False):
                logger.info("Slot '{}' challenge successfully resolved", slot_id)
                fetcher = getattr(self.http_pool, "_fetchers", {}).get(slot_id) if self.http_pool else None
                await reload_recovered_profile(self, fetcher, req.client_identity)
                # Activation already set READY; in the new policy only a valid poll resets backoff.
                if self.slot_signal_handler is not None and not self.identity_cleanup:
                    await self.slot_signal_handler(slot_id, SlotSignal.CHALLENGE_SOLVED)
            elif getattr(outcome, "status", None) == ChallengeStatus.OPERATOR_REQUIRED:
                logger.warning("Slot '{}' challenge requires operator intervention: {}", slot_id, getattr(outcome, "reason", ""))
            else:
                logger.warning("Slot '{}' challenge resolution failed: status={} reason={}", slot_id, getattr(outcome, "status", ""), getattr(outcome, "reason", ""))
            return outcome
        except HttpDeferred:
            raise
        except Exception as exc:
            logger.exception("Slot '{}' unhandled exception during challenge handling: {}", slot_id, exc)
            return None

    async def _handle_signal(
        self, assignment: QueryAssignment, response: Any, classified: Any, quarantine_duration_sec: float | None = None,
    ) -> bool:
        signal = classified.suggested_signal
        if signal is None or signal == SlotSignal.PROBE_OK:
            return False
        logger.warning("Slot signal={} for slot={} (status={}, reason={})", signal, assignment.slot_id, response.status_code, getattr(classified, "details", ""))
        if signal == SlotSignal.CHALLENGE_DETECTED and self.challenge_service and should_attempt_challenge(response):
            outcome = await self._handle_challenge(assignment, response)
            if getattr(outcome, "solved", False):
                return True
        await self._report_slot_failure(assignment, signal, quarantine_duration_sec=quarantine_duration_sec)
        return False

    async def _retry_fetch(
        self, assignment: QueryAssignment, fetcher: Any, target_url: str | None = None,
        hdrs: dict[str, str] | None = None, client_identity: str | None = None,
    ) -> tuple[Any, Any]:
        await reload_recovered_profile(self, fetcher, client_identity)
        url = target_url or assignment.canonical_url
        headers = hdrs if target_url else None
        if not target_url:
            url, headers = resolve_poll_target(assignment.canonical_url)
        resp = await safe_fetch_attempt(fetcher, to_wire_poll_url(url), headers, client_identity=client_identity, purpose="retry")
        record_poll_response(assignment.canonical_url, resp.text, resp.status_code)
        logger.info(
            "Public poll retry slot={} fetcher={} status={} len={} err={} cid={}",
            assignment.slot_id, type(fetcher).__name__, resp.status_code, len(resp.text or ""), resp.error, client_identity,
        )
        cls = classify_response(resp.status_code, resp.text, resp.url, require_auth=self.require_auth)
        return resp, cls

    async def _handle_challenge_retry(
        self, assignment: QueryAssignment, fetcher: Any, target_url: str,
        hdrs: dict[str, str] | None, origin_cid: str | None,
    ) -> tuple[Any, Any] | None:
        from hunter.application.poll_attempts import execute_retry_with_peer_failover
        return await execute_retry_with_peer_failover(self, assignment, fetcher, target_url, hdrs, origin_cid)

    async def poll(self, assignment: QueryAssignment, sequence: int) -> Any:
        if self.conn_mgr:
            poll_state = await _assignment_poll_state(self.conn_mgr, assignment)
            if poll_state != "current":
                if poll_state == "stale": await self.refresh_query(assignment.query_id)
                return None
        fetcher = self.http_pool.get_fetcher(assignment.slot_id, assignment.endpoint)
        target_url, hdrs = resolve_poll_target(assignment.canonical_url)
        response = await safe_fetch_attempt(fetcher, to_wire_poll_url(target_url), hdrs)
        origin_cid = getattr(response, "client_identity", None)
        if can_fallback_to_html(target_url, assignment.canonical_url, response):
            invalidate_poll_target(assignment.canonical_url)
            target_url, hdrs = assignment.canonical_url, None
            response = await safe_fetch_attempt(fetcher, to_wire_poll_url(target_url), None, client_identity=origin_cid)
        record_poll_response(assignment.canonical_url, response.text, response.status_code)
        logger.info(
            "Public poll slot={} fetcher={} status={} len={} err={} cid={}",
            assignment.slot_id, type(fetcher).__name__, response.status_code, len(response.text or ""), response.error, origin_cid,
        )
        ra_val = extract_retry_after(response)
        if response.error or response.status_code >= 500:
            _record_poll_metrics(self.metrics, assignment, response, error=True, source_visibility="unavailable")
            await self._report_slot_failure(assignment, SlotSignal.TIMEOUT, quarantine_duration_sec=ra_val)
            return None
        classified = classify_response(response.status_code, response.text, response.url, require_auth=self.require_auth)
        has_peer = has_peer_identity(fetcher, peer_identity(origin_cid))
        is_chal = classified.suggested_signal == SlotSignal.CHALLENGE_DETECTED
        is_barrier = is_chal or classified.suggested_signal in (SlotSignal.HTTP_403, SlotSignal.HTTP_429)
        if self.challenge_service and ((is_chal and should_attempt_challenge(response)) or (is_barrier and has_peer)):
            ret = await execute_challenge_with_peer_failover(self, assignment, fetcher, target_url, hdrs, response, origin_cid)
            if ret is None:
                _record_poll_metrics(self.metrics, assignment, response, error=True, source_visibility="unavailable")
                return None
            response, classified = ret
        else:
            challenge_solved = await self._handle_signal(assignment, response, classified, quarantine_duration_sec=ra_val)
            if challenge_solved:
                ret = await self._handle_challenge_retry(assignment, fetcher, target_url, hdrs, origin_cid)
                if ret is None:
                    return None
                response, classified = ret
            elif classified.suggested_signal not in (None, SlotSignal.PROBE_OK):
                _record_poll_metrics(self.metrics, assignment, response, error=True, source_visibility="unavailable")
                return None
        outcome = _parse_outcome(classified, response.status_code)
        if outcome is None:
            if "/web/1/js/items" in target_url: invalidate_ambiguous_poll_target(assignment.canonical_url)
            _record_poll_metrics(self.metrics, assignment, response, error=True, source_visibility="unavailable")
            return None
        batch = assemble_poll_batch(assignment, response, outcome, None, self.metrics)
        seq = await self.next_sequence() if self.next_sequence else sequence
        res = await self.ingest(batch, request_sequence=seq)
        if self.slot_signal_handler is not None:
            with suppress(Exception):
                await self.slot_signal_handler(assignment.slot_id, SlotSignal.PROBE_OK, query_id=assignment.query_id, assignment_generation=assignment.generation)
        return res

    async def refresh(self, *, start: bool = True) -> None:
        async with self._refresh_lock:
            if self._is_stopped:
                return
            for worker in (*self.workers.values(), *self.retiring_workers): await worker.stop()
            self.retiring_workers.clear()
            self.workers.clear()
            assignments = await load_active_assignments(self.conn_mgr)
            if self._is_stopped:
                return
            grouped: dict[str, list[QueryAssignment]] = {}
            for assignment in assignments:
                grouped.setdefault(assignment.slot_id, []).append(assignment)
            for slot_id, entries in grouped.items():
                if self._is_stopped:
                    return
                await self._replace_slot(slot_id, entries, start=start)

    async def _replace_slot(
        self, slot_id: str, entries: list[QueryAssignment], *, start: bool
    ) -> None:
        if not entries or self._is_stopped:
            return
        fetcher = self.http_pool.get_fetcher(slot_id, entries[0].endpoint)
        res = fetcher.load_cookies_from_store()
        if inspect.isawaitable(res): await res
        if self._is_stopped:
            return
        old_entries = {k[1]: v for k, v in self.assignments.items() if k[0] == slot_id}
        for key in list(self.assignments):
            if key[0] == slot_id: self.assignments.pop(key, None)
        active_entries: list[QueryAssignment] = []
        for item in entries:
            existing = old_entries.get(item.query_id)
            target = existing if (existing and existing.generation > item.generation) else item
            self.assignments[(slot_id, target.query_id)] = target
            active_entries.append(target)

        async def handler(query_id: str, sequence: int) -> Any:
            current = self.assignments.get((slot_id, query_id))
            return await self.poll(current, sequence) if current else None

        worker = SlotWorker(
            slot_id, [entry.query_id for entry in active_entries], poll_policy=self.policy,
            poll_handler=handler, is_paused=self.is_paused,
        )
        self.workers[slot_id] = worker
        if start and not self._is_stopped:
            await worker.start()

    async def refresh_query(self, query_id: str) -> None:
        """Reconcile only slots whose persisted assignment set changed for this query."""
        async with self._refresh_lock:
            if self._is_stopped:
                return
            current = await load_active_assignments(self.conn_mgr)
            if self._is_stopped:
                return
            grouped: dict[str, list[QueryAssignment]] = {}
            for item in current:
                grouped.setdefault(item.slot_id, []).append(item)
            old_slots = {slot for slot, qid in self.assignments if qid == query_id}
            new_slots = {item.slot_id for item in current if item.query_id == query_id}
            for slot_id in old_slots | new_slots:
                old = sorted((v.query_id, v.generation) for k, v in self.assignments.items() if k[0] == slot_id)
                entries = grouped.get(slot_id, [])
                new = sorted((item.query_id, item.generation) for item in entries)
                worker = self.workers.get(slot_id)
                worker_alive = worker is not None and getattr(worker, "_running", False)
                if old == new and worker_alive:
                    continue
                if not entries:
                    if worker:
                        is_current_worker = worker._task is asyncio.current_task()
                        await worker.stop()
                        self.workers.pop(slot_id, None)
                        if is_current_worker:
                            self.retiring_workers.add(worker)
                    self.assignments = {k: v for k, v in self.assignments.items() if k[0] != slot_id}
                elif worker and worker_alive:
                    old_entries = {k[1]: v for k, v in self.assignments.items() if k[0] == slot_id}
                    self.assignments = {k: v for k, v in self.assignments.items() if k[0] != slot_id}
                    for item in entries:
                        existing = old_entries.get(item.query_id)
                        target = existing if (existing and existing.generation > item.generation) else item
                        self.assignments[(slot_id, target.query_id)] = target
                    worker.update_assigned_queries([k[1] for k in self.assignments if k[0] == slot_id])
                else:
                    await self._replace_slot(slot_id, entries, start=True)

    async def stop(self) -> None:
        self._is_stopped = True
        try:
            async with self._refresh_lock:
                for w in (*self.workers.values(), *self.retiring_workers): await w.stop()
                self.workers.clear()
                self.retiring_workers.clear()
        except asyncio.CancelledError:
            for w in (*self.workers.values(), *self.retiring_workers): await w.stop()
            self.workers.clear()
            self.retiring_workers.clear()
            raise
