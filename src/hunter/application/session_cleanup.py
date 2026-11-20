"""Destructive cleanup requires explicit invalidity and unchanged origin provenance."""
import inspect
import asyncio

from loguru import logger

from hunter.transport.challenge import ChallengeStatus
from hunter.transport.recovery_identity import RecoveryFailureKind, canonical_identity, resolve_session_slot_id
from hunter.transport.session_store import SessionStoreError


def _store(runtime):
    return (getattr(runtime.challenge_service, "session_store", None)
            or getattr(runtime.http_pool, "session_store", None))


def outcome_matches_request(request, outcome):
    identity = canonical_identity(request.client_identity)
    return (identity is not None and getattr(outcome, "slot_id", None) == request.slot_id
            and canonical_identity(getattr(outcome, "client_identity", None)) == identity)


async def capture_origin_profile(runtime, request):
    identity = canonical_identity(request.client_identity)
    store = _store(runtime)
    if identity is None or store is None or request.endpoint is None:
        return None
    try:
        profile = await store.load_session(resolve_session_slot_id(request.slot_id, identity),
                                           expected_endpoint=request.endpoint)
    except (SessionStoreError, ValueError):
        logger.warning("Cannot capture origin profile fence; automatic cleanup disabled for this attempt")
        return None
    return (profile.metadata.version, profile.metadata.generation) if profile is not None else None


async def reload_recovered_profile(runtime, fetcher, identity):
    if fetcher is None:
        return
    if runtime.identity_cleanup:
        identity = canonical_identity(identity)
        if identity is None or not hasattr(fetcher, "load_identity_from_store"):
            logger.warning("Cannot reload one confirmed identity; shared profiles left unchanged")
            return
        result = fetcher.load_identity_from_store(identity)
    elif hasattr(fetcher, "load_cookies_from_store"):
        result = fetcher.load_cookies_from_store()
    else:
        return
    if inspect.isawaitable(result):
        await result


async def cleanup_after_outcome(runtime, request, outcome, origin_fence):
    store = _store(runtime)
    if store is None or getattr(outcome, "solved", False):
        return
    fetcher = getattr(runtime.http_pool, "_fetchers", {}).get(request.slot_id)
    if not runtime.identity_cleanup:
        if getattr(outcome, "status", None) != ChallengeStatus.OPERATOR_REQUIRED:
            return
        await store.delete_session(request.slot_id)
        try:
            await store.delete_session(f"{request.slot_id}__user_b")
        except Exception:
            logger.warning("Legacy secondary profile deletion failed")
        if fetcher and hasattr(fetcher, "load_cookies_from_store"):
            result = fetcher.load_cookies_from_store()
            if inspect.isawaitable(result):
                await result
        return
    identity = canonical_identity(request.client_identity)
    if (identity is None or origin_fence is None
            or request.expected_generation is None
            or canonical_identity(getattr(outcome, "client_identity", None)) != identity
            or getattr(outcome, "slot_id", None) != request.slot_id
            or getattr(outcome, "failure_kind", None) != RecoveryFailureKind.INVALID_PROFILE):
        return
    sid = resolve_session_slot_id(request.slot_id, identity)
    slot_fence = (request.slot_id, request.expected_generation, request.endpoint)
    await _drain_on_cancel(_delete_and_reset(store, sid, origin_fence, slot_fence, fetcher, identity))


async def _delete_and_reset(store, sid, fence, slot_fence, fetcher, identity):
    deleted = await store.delete_session_if_matches(sid, *fence, slot_fence=slot_fence)
    if deleted and fetcher is not None and hasattr(fetcher, "reset_identity_in_memory"):
        result = fetcher.reset_identity_in_memory(identity)
        if inspect.isawaitable(result):
            await result


async def _drain_on_cancel(operation):
    task = asyncio.create_task(operation)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as cancelled:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.error("Profile cleanup failed during cancellation ({})", type(error).__name__)
            raise cancelled from error
        raise cancelled
