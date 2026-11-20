"""Bounded poll attempt execution, retry identity, and fallback policy (T-971, T-972)."""

from __future__ import annotations

from datetime import UTC, datetime
import dataclasses
import inspect
from typing import Any

import asyncio
from loguru import logger
from hunter.application.metrics import EventLatencyProvenance, LowCardinalityMetricsCollector
from hunter.collect.parsers.search import parse_search
from hunter.domain.observations import ObservationBatch, ObservedListing, ParseOutcome, PriceState
from hunter.domain.slots import SlotSignal
from hunter.transport.classification import ResponseOutcome, is_ip_level_block
from hunter.transport.recovery_identity import CaseInsensitiveDict, canonical_identity, peer_identity
from hunter.transport.attempt_scope import client_scopes


async def safe_fetch_attempt(
    fetcher: Any, url: str, headers: dict[str, str] | None = None, client_identity: str | None = None, purpose: str = "poll"
) -> Any:
    """Invokes fetcher.fetch safely forwarding headers and pinned client identity."""
    fetch_fn = getattr(fetcher, "fetch", None)
    if fetch_fn is None:
        raise AttributeError(f"{fetcher!r} object has no attribute 'fetch'")
    kwargs: dict[str, Any] = {}
    try:
        sig = inspect.signature(fetch_fn)
        params = sig.parameters
        if "headers" in params or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
            kwargs["headers"] = headers
        if "client_identity" in params or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
            if client_identity is not None:
                kwargs["client_identity"] = client_identity
    except (ValueError, TypeError):
        kwargs["headers"] = headers

    # Pre-fetch wire header snapshot (captures client UA before dispatch/mutation)
    client_b = getattr(fetcher, "curl_client_b", None)
    client_a = getattr(fetcher, "curl_client", None) or getattr(fetcher, "client", None)
    pre_b = dict(getattr(client_b, "headers", {}) or {})
    pre_a = dict(getattr(client_a, "headers", {}) or {})

    with client_scopes([client_a, client_b], purpose):
        resp = await fetch_fn(url, **kwargs)

    cid = getattr(resp, "client_identity", None) or client_identity
    chosen = pre_b if cid in ("user_b", "b") else pre_a
    wire_hdrs = CaseInsensitiveDict(chosen)
    if headers:
        wire_hdrs.update(headers)
    if not getattr(resp, "request_headers", None):
        try:
            resp = dataclasses.replace(resp, request_headers=wire_hdrs) if dataclasses.is_dataclass(resp) and hasattr(resp, "request_headers") else resp
            if not dataclasses.is_dataclass(resp): setattr(resp, "request_headers", wire_hdrs)
        except Exception:
            pass
    return resp


def has_retry_after(response: Any) -> bool:
    """Returns True if response contains a Retry-After header."""
    return any(k.lower() == "retry-after" for k in (getattr(response, "headers", None) or {}))


def extract_retry_after(response: Any) -> float | None:
    """Parses Retry-After header value in seconds from response headers if present."""
    val = next((v for k, v in (getattr(response, "headers", None) or {}).items() if k.lower() == "retry-after"), None)
    if val:
        from hunter.domain.recovery_policy import parse_retry_after
        return parse_retry_after(val)
    return None


def can_fallback_to_html(target_url: str, canonical_url: str, response: Any) -> bool:
    """Evaluates whether bounded HTML fallback is permitted (404/410 only, no Retry-After)."""
    if not target_url or target_url == canonical_url:
        return False
    status = getattr(response, "status_code", 0)
    return status in (404, 410) and not has_retry_after(response) and not getattr(response, "error", None)


def classify_retry_signal(response: Any, classified: Any) -> SlotSignal | None:
    """Extracts failure signal for secondary retry response without recursive solve."""
    if getattr(response, "error", None): return SlotSignal.TIMEOUT
    status = getattr(response, "status_code", 0)
    if status >= 500: return SlotSignal.TIMEOUT
    sig = getattr(classified, "suggested_signal", None)
    if sig and sig != SlotSignal.PROBE_OK: return sig
    if status == 429: return SlotSignal.HTTP_429
    if status == 403: return SlotSignal.HTTP_403
    if status == 439: return SlotSignal.CHALLENGE_DETECTED
    return None


def has_peer_identity(fetcher: Any, peer_cid: str | None) -> bool:
    if not peer_cid or fetcher is None:
        return False
    canon = canonical_identity(peer_cid)
    if canon is None:
        return False
    is_quarantined = getattr(fetcher, "is_identity_quarantined", None)
    if callable(is_quarantined) and is_quarantined(canon):
        return False
    client_attr = "curl_client_b" if canon == "user_b" else "curl_client"
    return getattr(fetcher, client_attr, None) is not None


async def _execute_peer_challenge_attempt(
    runtime: Any, assignment: Any, fetcher: Any, target_url: str,
    hdrs: dict[str, str] | None, peer_cid: str,
) -> tuple[tuple[Any, Any] | None, Any, Any]:
    peer_resp, peer_cls = await runtime._retry_fetch(
        assignment, fetcher, target_url=target_url, hdrs=hdrs, client_identity=peer_cid,
    )
    peer_sig = classify_retry_signal(peer_resp, peer_cls)
    if not peer_sig:
        logger.info("Peer identity '{}' successfully recovered poll for slot='{}'", peer_cid, assignment.slot_id)
        return (peer_resp, peer_cls), peer_resp, peer_sig
    from hunter.transport.challenge import should_attempt_challenge
    if peer_sig == SlotSignal.CHALLENGE_DETECTED and runtime.challenge_service and should_attempt_challenge(peer_resp):
        logger.warning("Peer identity '{}' encountered challenge; attempting solver", peer_cid)
        peer_outcome = await runtime._handle_challenge(assignment, peer_resp, allow_quarantine=True, client_identity=peer_cid)
        if getattr(peer_outcome, "solved", False):
            await asyncio.sleep(1.0)
            ret_resp, ret_cls = await runtime._retry_fetch(assignment, fetcher, target_url=target_url, hdrs=hdrs, client_identity=peer_cid)
            ret_sig = classify_retry_signal(ret_resp, ret_cls)
            if not ret_sig:
                logger.info("Peer identity '{}' solved challenge and recovered poll", peer_cid)
                return (ret_resp, ret_cls), ret_resp, ret_sig
            return None, ret_resp, ret_sig
    return None, peer_resp, peer_sig


async def execute_challenge_with_peer_failover(
    runtime: Any,
    assignment: Any,
    fetcher: Any,
    target_url: str,
    hdrs: dict[str, str] | None,
    initial_response: Any,
    origin_cid: str | None,
) -> tuple[Any, Any] | None:
    """Executes challenge resolution and retry with peer failover on solver failure."""
    if target_url != assignment.canonical_url:
        from hunter.transport.catalog_api import invalidate_poll_target, _DEFAULT_RESOLVER
        if not getattr(_DEFAULT_RESOLVER, "preserve_access_routes", False):
            invalidate_poll_target(assignment.canonical_url)
            target_url, hdrs = assignment.canonical_url, None

    peer_cid = peer_identity(origin_cid)
    has_peer = has_peer_identity(fetcher, peer_cid)

    from hunter.transport.challenge import should_attempt_challenge
    if should_attempt_challenge(initial_response):
        outcome = await runtime._handle_challenge(assignment, initial_response, allow_quarantine=not has_peer, client_identity=origin_cid)
        if getattr(outcome, "solved", False):
            return await execute_retry_with_peer_failover(runtime, assignment, fetcher, target_url, hdrs, origin_cid)
        logger.warning("Challenge solver failed on cid='{}' (status={}, reason='{}')", origin_cid, getattr(outcome, "status", None), getattr(outcome, "reason", ""))
    else:
        logger.warning("Tokenless barrier on cid='{}' (status={}); skipping solver", origin_cid, getattr(initial_response, "status_code", None))
    last_resp, last_sig = initial_response, SlotSignal.CHALLENGE_DETECTED
    if has_peer and peer_cid and not is_ip_level_block(initial_response):
        await asyncio.sleep(4.0)
        logger.warning(
            "Attempting challenge peer failover from cid='{}' to cid='{}' for slot='{}'",
            origin_cid, peer_cid, assignment.slot_id,
        )
        res, last_resp, last_sig = await _execute_peer_challenge_attempt(
            runtime, assignment, fetcher, target_url, hdrs, peer_cid,
        )
        if res is not None:
            return res
    elif is_ip_level_block(initial_response):
        logger.warning("IP-level block detected on slot='{}' cid='{}'; skipping peer retry on same IP", assignment.slot_id, origin_cid)

    logger.warning("All challenge recovery attempts failed for slot='{}'; slot quarantined", assignment.slot_id)
    ra_sec = extract_retry_after(last_resp) if last_resp else None
    await runtime._report_slot_failure(assignment, last_sig or SlotSignal.CHALLENGE_DETECTED, quarantine_duration_sec=ra_sec if ra_sec is not None else 62.0)
    return None


async def execute_retry_with_peer_failover(
    runtime: Any,
    assignment: Any,
    fetcher: Any,
    target_url: str,
    hdrs: dict[str, str] | None,
    origin_cid: str | None,
) -> tuple[Any, Any] | None:
    """Executes origin challenge retry with symmetrical peer identity failover."""
    from hunter.transport.catalog_api import invalidate_poll_target, _DEFAULT_RESOLVER
    preserve = getattr(_DEFAULT_RESOLVER, "preserve_access_routes", False)
    retry_url = (assignment.canonical_url if not preserve else target_url) if target_url != assignment.canonical_url else target_url
    retry_hdrs = (None if not preserve else hdrs) if retry_url == assignment.canonical_url else hdrs
    if target_url != assignment.canonical_url and not preserve:
        invalidate_poll_target(assignment.canonical_url)

    await asyncio.sleep(3.5)
    resp, classified = await runtime._retry_fetch(
        assignment, fetcher, target_url=retry_url, hdrs=retry_hdrs, client_identity=origin_cid,
    )
    sec_sig = classify_retry_signal(resp, classified)
    if not sec_sig:
        return resp, classified

    peer_cid = peer_identity(origin_cid)
    has_peer = has_peer_identity(fetcher, peer_cid)

    if has_peer and peer_cid and not is_ip_level_block(resp):
        logger.warning(
            "Primary retry on cid='{}' failed (signal={}, status={}); attempting peer failover to cid='{}'",
            origin_cid, sec_sig, getattr(resp, "status_code", 0), peer_cid,
        )
        await asyncio.sleep(4.0)
        peer_resp, peer_cls = await runtime._retry_fetch(
            assignment, fetcher, target_url=retry_url, hdrs=retry_hdrs, client_identity=peer_cid,
        )
        peer_sig = classify_retry_signal(peer_resp, peer_cls)
        if not peer_sig:
            logger.info("Peer identity '{}' successfully recovered poll for slot='{}'", peer_cid, assignment.slot_id)
            return peer_resp, peer_cls
        if peer_sig == SlotSignal.CHALLENGE_DETECTED and runtime.challenge_service:
            logger.warning("Peer identity '{}' encountered challenge on retry failover; solving", peer_cid)
            peer_outcome = await runtime._handle_challenge(assignment, peer_resp, allow_quarantine=True, client_identity=peer_cid)
            if getattr(peer_outcome, "solved", False):
                await asyncio.sleep(3.5)
                ret_resp, ret_cls = await runtime._retry_fetch(assignment, fetcher, target_url=retry_url, hdrs=retry_hdrs, client_identity=peer_cid)
                ret_sig = classify_retry_signal(ret_resp, ret_cls)
                if not ret_sig:
                    logger.info("Peer identity '{}' solved challenge and recovered poll on failover", peer_cid)
                    return ret_resp, ret_cls
                peer_resp, peer_cls, peer_sig = ret_resp, ret_cls, ret_sig
        resp, classified, sec_sig = peer_resp, peer_cls, peer_sig

    _record_poll_metrics(runtime.metrics, assignment, resp, error=True, source_visibility="unavailable")
    ra_sec = extract_retry_after(resp)
    await runtime._report_slot_failure(assignment, sec_sig, quarantine_duration_sec=ra_sec)
    return None


def _listing_state(value: str) -> PriceState:
    try: return PriceState(value)
    except ValueError: return PriceState.ACTIVE if value == "ok" else PriceState.UNKNOWN


def _parse_outcome(result: Any, status_code: int) -> ParseOutcome | None:
    if status_code == 429: return ParseOutcome.RATE_LIMIT_429
    if status_code == 403: return ParseOutcome.BLOCKED_403
    return {
        ResponseOutcome.OK: ParseOutcome.OK,
        ResponseOutcome.EXPLICIT_EMPTY: ParseOutcome.EMPTY,
        ResponseOutcome.CHALLENGE: ParseOutcome.CAPTCHA,
    }.get(result.outcome)


def _record_poll_metrics(
    metrics: LowCardinalityMetricsCollector, assignment: Any, response: Any,
    *, error: bool, source_visibility: str, publication_lag_sec: float | None = None,
) -> None:
    metrics.record_poll(
        slot_id=assignment.slot_id, status_code=response.status_code, error=error,
        headers=getattr(response, "headers", {}), source_visibility=source_visibility,
        publication_lag_sec=publication_lag_sec, duration_sec=response.elapsed_sec,
        wire_body_bytes=getattr(response, "wire_body_bytes", None),
    )


def _listing_photo(hit: Any) -> str | None:
    photos = getattr(hit, "extra", {}).get("photos") if hasattr(hit, "extra") and isinstance(hit.extra, dict) else None
    if isinstance(photos, list) and photos and isinstance(photos[0], str) and photos[0].startswith("http"):
        return photos[0]
    return None


def _listing_snippet(hit: Any) -> dict[str, Any] | None:
    ex = getattr(hit, "extra", {}) or {}
    s: dict[str, Any] = {}
    desc = getattr(hit, "snippet", None) or ex.get("description_preview")
    if desc: s["description"] = desc
    if isinstance(ex.get("photos"), list) and ex["photos"]: s["photos"] = ex["photos"]
    if ex.get("rating_score") is not None: s["seller_rating"] = ex["rating_score"]
    if ex.get("rating_reviews"): s["seller_reviews"] = ex["rating_reviews"]
    if ex.get("reserved_via_delivery") or ex.get("is_reserved"): s["is_reserved"] = True
    if ex.get("delivery") is not None: s["delivery"] = bool(ex["delivery"])
    if getattr(hit, "seller_type", None): s["seller_type"] = hit.seller_type
    return s or None


def _observed_listings(hits: Any) -> tuple[ObservedListing, ...]:
    return tuple(
        ObservedListing(
            avito_id=hit.avito_id, title=hit.title or "", price=hit.price,
            price_state=_listing_state(hit.price_state), url=hit.url,
            location=hit.city, published_at=hit.published_at,
            image_url=_listing_photo(hit), raw_snippet=_listing_snippet(hit),
        )
        for hit in hits
    )


def _publication_lag(listings: tuple[ObservedListing, ...], observed_at: str) -> float | None:
    lags = [
        lag for listing in listings
        if (lag := EventLatencyProvenance(published_at=listing.published_at, observed_at=observed_at).compute_publication_lag()) is not None
    ]
    return min(lags) if lags else None


async def prepare_challenge_request(
    asg_or_id: str | Any,
    response: Any,
    http_pool: Any,
    challenge_service: Any,
    allow_quarantine: bool = True,
    client_identity: str | None = None,
) -> Any:
    """Prepares structured ChallengeRequest with origin context and decoupled generations."""
    from hunter.collect.firewall_pow import extract_pow_challenge
    from hunter.transport.challenge import ChallengeRequest

    if isinstance(asg_or_id, str):
        slot_id, exp_gen, asg_gen, q_id, ep = asg_or_id, None, None, None, None
        slot_repo = getattr(challenge_service, "slot_repo", None)
        if slot_repo and hasattr(slot_repo, "get_slot"):
            res = slot_repo.get_slot(slot_id)
            if inspect.isawaitable(res):
                cur = await res
                if cur:
                    exp_gen = getattr(cur, "generation", None)
    else:
        slot_id = asg_or_id.slot_id
        asg_gen = asg_or_id.generation
        q_id = asg_or_id.query_id
        ep = asg_or_id.endpoint
        exp_gen = getattr(asg_or_id, "slot_generation", None)
        if not exp_gen:
            exp_gen = asg_or_id.generation

    cid = client_identity or getattr(response, "client_identity", "")
    fetcher = getattr(http_pool, "_fetchers", {}).get(slot_id) if http_pool else None
    borrowed_client, proxy_creds = None, None
    req_hdrs = CaseInsensitiveDict()
    if getattr(response, "request_headers", None):
        req_hdrs.update(response.request_headers)
    elif getattr(asg_or_id, "request_headers", None):
        req_hdrs.update(asg_or_id.request_headers)
    if fetcher is not None:
        proxy_creds = getattr(fetcher, "proxy_credentials", None)
        borrowed_client = (
            getattr(fetcher, "curl_client_b", None) if cid in ("user_b", "b") else getattr(fetcher, "curl_client", None)
        ) or getattr(fetcher, "client", None)
        if "User-Agent" not in req_hdrs and borrowed_client and hasattr(borrowed_client, "headers"):
            ua = borrowed_client.headers.get("User-Agent") if hasattr(borrowed_client.headers, "get") else None
            if not ua and hasattr(borrowed_client.headers, "items"):
                ua = next((v for k, v in borrowed_client.headers.items() if k.lower() == "user-agent"), None)
            if ua:
                req_hdrs["User-Agent"] = ua

    text = getattr(response, "text", "") if hasattr(response, "text") else (str(response) if response else "")
    headers, cookies = getattr(response, "headers", {}) or {}, getattr(response, "cookies", {}) or {}
    token = extract_pow_challenge(text, headers=headers, cookies=cookies)
    return ChallengeRequest(
        slot_id=slot_id, html=text, headers=req_hdrs, cookies=cookies, challenge_token=token,
        client_identity=cid, http_client=borrowed_client, endpoint=ep, proxy_credentials=proxy_creds,
        expected_generation=exp_gen, assignment_generation=asg_gen, query_id=q_id,
        allow_quarantine=allow_quarantine,
    )


def assemble_poll_batch(
    assignment: Any, response: Any, outcome: ParseOutcome,
    tenant_generations: dict[str, int] | None, metrics: LowCardinalityMetricsCollector,
) -> ObservationBatch:
    """Parses hits, records metrics, and constructs the observation batch."""
    hits = parse_search(response.text, base_url=assignment.canonical_url) if outcome == ParseOutcome.OK else []
    listings = _observed_listings(hits)
    observed_at = datetime.now(UTC).isoformat()
    poll_error = outcome not in {ParseOutcome.OK, ParseOutcome.EMPTY}
    _record_poll_metrics(
        metrics, assignment, response, error=poll_error,
        source_visibility=("unavailable" if poll_error else "visible" if listings else "empty"),
        publication_lag_sec=_publication_lag(listings, observed_at),
    )
    if outcome == ParseOutcome.OK and getattr(metrics, "valid_tracker", None):
        metrics.valid_tracker.record_valid_update(assignment.query_id, items_count=len(listings))
    return ObservationBatch(
        slot_id=assignment.slot_id, query_id=assignment.query_id, observed_at=observed_at,
        outcome=outcome, listings=listings, latency_ms=int(response.elapsed_sec * 1000),
        http_status=response.status_code, assignment_generation=assignment.generation,
        tenant_generations=tenant_generations,
    )
