"""Job lease fencing repository with conditional lease ack and renewal (S3 / T-467).

Contracts:
- Each lease assignment receives a cryptographically unique fencing token (UUID4).
- Expired lease re-issue receives a new, distinct token.
- Complete, fail, and renew operations strictly require the active fencing token.
- Stale workers with expired/mismatched tokens are rejected (no duplicate/ghost work).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    id: int
    kind: str
    product_id: str
    dedupe_key: str
    payload: dict[str, Any]
    priority: int
    attempts: int
    max_attempts: int
    lease_token: str
    lease_until: str


class JobClaimRepository:
    """Typed adapter over the Database facade's single job-lease implementation."""

    def __init__(self, db: Any) -> None:
        self.db = db

    async def claim_job(
        self,
        kind: str | None = None,
        lease_duration_sec: int = 120,
        kinds: list[str] | None = None,
    ) -> ClaimedJob | None:
        """Claims through Database and projects its row into the typed job model."""
        raw = await self.db.lease_job(
            kind=kind, lease_duration_sec=lease_duration_sec, kinds=kinds
        )
        if not raw:
            return None
        token = raw.get("lease_token")
        if not token:
            raise RuntimeError("Database returned a leased job without a fencing token")
        return ClaimedJob(
            id=int(raw["id"]),
            kind=str(raw["kind"]),
            product_id=str(raw["product_id"]),
            dedupe_key=str(raw["dedupe_key"]),
            payload=_parse_payload(raw.get("payload")),
            priority=int(raw["priority"]),
            attempts=int(raw["attempts"]),
            max_attempts=int(raw["max_attempts"]),
            lease_token=str(token),
            lease_until=str(raw["lease_until"]),
        )

    async def complete_job(
        self,
        job_id: int,
        lease_token: str,
        payload: dict[str, Any] | str | None = None,
    ) -> bool:
        """Completes only through the facade's active-token conditional update."""
        return await self.db.complete_job(
            job_id, payload=payload, lease_token=lease_token
        )

    async def fail_job(
        self,
        job_id: int,
        lease_token: str,
        error: str,
    ) -> str | None:
        """Fails only through the facade's active-token conditional transition."""
        return await self.db.fail_job(job_id, error, lease_token=lease_token)

    async def renew_lease(
        self,
        job_id: int,
        lease_token: str,
        extend_sec: int = 60,
    ) -> bool:
        """Renews only through the facade's active-token conditional update."""
        return await self.db.renew_job_lease(
            job_id, lease_token, extend_sec=extend_sec
        )


def _parse_payload(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return {}
    import json

    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


class JobLeaseLostError(RuntimeError):
    """Signals that active work was cancelled after lease ownership became uncertain."""


async def renew_job_lease_or_raise(
    db: Any, job_id: int, lease_token: str, extend_sec: int = 60
) -> None:
    """Atomically verify current ownership before a job-owned effect."""
    if not lease_token:
        raise JobLeaseLostError(f"Job #{job_id} missing fencing token")
    try:
        renew = getattr(db, "renew_lease", None) or db.renew_job_lease
        if not await renew(job_id, lease_token, extend_sec=extend_sec):
            raise JobLeaseLostError(f"Job #{job_id} lease ownership lost before effect")
    except JobLeaseLostError:
        raise
    except Exception as exc:
        raise JobLeaseLostError(f"Job #{job_id} lease could not be verified") from exc


def fence_job_effect(db: Any, job_id: int, lease_token: str, effect: Any) -> Any:
    """Renew the active lease immediately before an effect that belongs to its job."""
    async def _run(*args: Any, **kwargs: Any) -> Any:
        await renew_job_lease_or_raise(db, job_id, lease_token)
        return await effect(*args, **kwargs)

    return _run


@contextlib.asynccontextmanager
async def job_lease_heartbeat(
    repo_or_db: Any,
    job_id: int,
    lease_token: str,
    interval_sec: float = 30.0,
    extend_sec: int = 60,
):
    """Periodically renews job lease while task is active, bounded strictly to task lifetime."""
    await renew_job_lease_or_raise(repo_or_db, job_id, lease_token, extend_sec)
    stop_event = asyncio.Event()
    ownership_lost = asyncio.Event()
    owner_task = asyncio.current_task()

    async def _heartbeat_loop() -> None:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval_sec)
                break
            except asyncio.TimeoutError:
                pass
            try:
                await renew_job_lease_or_raise(
                    repo_or_db, job_id, lease_token, extend_sec
                )
            except Exception as exc:
                logger.error("Error during job #%s heartbeat renewal: %s", job_id, exc)
                ownership_lost.set()
                if owner_task is not None:
                    owner_task.cancel()
                break

    hb_task = asyncio.create_task(_heartbeat_loop())
    try:
        yield
    except asyncio.CancelledError as exc:
        if ownership_lost.is_set():
            raise JobLeaseLostError(f"Job #{job_id} lease ownership lost") from exc
        raise
    finally:
        stop_event.set()
        hb_task.cancel()
        try:
            await hb_task
        except asyncio.CancelledError:
            pass

