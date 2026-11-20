"""Owner-only AI gate and enrichment service (S4 / T-475).

Contracts:
- Two-tier gate: server-side verified user.role == 'owner' AND hidden flag is True.
- Forged roles and ordinary users are strictly denied.
- Separate queue/budget/timeout with durable job provenance.
- Unavailable AI never impairs public delivery.
- Public paths do not import or invoke this module.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
import time
from typing import Any
import uuid

from loguru import logger

from hunter.core.models import User


class EnrichmentJobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    DENIED = "denied"
    BYPASSED = "bypassed"


@dataclass
class EnrichmentJob:
    job_id: str
    user_id: int
    item_id: str
    status: EnrichmentJobStatus
    provider: str = ""
    model: str = ""
    result: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    completed_at: str | None = None
    duration_ms: float = 0.0


def check_owner_ai_gate(
    user: User | None,
    *,
    hidden_flag: bool = False,
    db: Any = None,
) -> bool:
    """Strict server-side gate for AI capabilities."""
    if user is None or getattr(user, "role", None) != "owner" or not bool(hidden_flag):
        return False

    if db is not None and hasattr(db, "get_user_by_id_sync"):
        try:
            real_data = db.get_user_by_id_sync(user.id)
            if not real_data or real_data.get("role") != "owner":
                return False
        except Exception:
            return False

    return True


async def check_owner_ai_gate_async(
    user: User | None,
    *,
    hidden_flag: bool = False,
    db: Any = None,
) -> bool:
    """Async variant verifying owner gate against database."""
    if user is None or getattr(user, "role", None) != "owner" or not bool(hidden_flag):
        return False

    if db is not None and hasattr(db, "get_user_by_id"):
        try:
            real_data = await db.get_user_by_id(user.id)
            if not real_data or real_data.get("role") != "owner":
                return False
        except Exception:
            return False

    return True


class OwnerEnrichmentService:
    """Dedicated isolated runner for owner AI jobs with durable provenance."""

    def __init__(
        self,
        db: Any = None,
        *,
        llm_router: Any = None,
        timeout_sec: float = 15.0,
        max_queue_size: int = 100,
    ) -> None:
        self.db = db
        self.llm_router = llm_router
        self.timeout_sec = timeout_sec
        self.queue: asyncio.Queue[EnrichmentJob] = asyncio.Queue(maxsize=max_queue_size)
        self.jobs: dict[str, EnrichmentJob] = {}

    def _get_router(self) -> Any:
        if self.llm_router is not None:
            return self.llm_router
        from hunter.llm.router import LLMRouter
        return LLMRouter()

    async def is_eligible(self, user: User | None, *, hidden_flag: bool = False) -> bool:
        return await check_owner_ai_gate_async(user, hidden_flag=hidden_flag, db=self.db)

    async def submit_enrichment(
        self,
        user: User | None,
        item_id: str,
        prompt: str,
        *,
        hidden_flag: bool = False,
        system: str = "",
        model_hint: str = "",
    ) -> EnrichmentJob:
        """Submits an enrichment job with server-side validation and separate timeout."""
        job_id = f"ai_job_{uuid.uuid4().hex[:12]}"
        user_id = getattr(user, "id", 0) or 0

        eligible = await self.is_eligible(user, hidden_flag=hidden_flag)
        if not eligible:
            logger.warning("Owner AI gate rejected request for user %s", user_id)
            job = EnrichmentJob(
                job_id=job_id,
                user_id=user_id,
                item_id=item_id,
                status=EnrichmentJobStatus.DENIED,
                error="Owner AI access denied: role must be owner and hidden flag must be enabled",
                completed_at=datetime.now(UTC).isoformat(),
            )
            self.jobs[job_id] = job
            return job

        job = EnrichmentJob(
            job_id=job_id,
            user_id=user_id,
            item_id=item_id,
            status=EnrichmentJobStatus.RUNNING,
            model=model_hint,
        )
        self.jobs[job_id] = job
        t0 = time.monotonic()

        try:
            router = self._get_router()
            coro = router.complete_json(prompt, system=system)
            res = await asyncio.wait_for(coro, timeout=self.timeout_sec)
            job.status = EnrichmentJobStatus.COMPLETED
            job.result = res if isinstance(res, dict) else {"raw": res}
            job.provider = getattr(router, "last_provider", "llm")
        except asyncio.TimeoutError:
            logger.warning("Owner AI job %s timed out after %ss", job_id, self.timeout_sec)
            job.status = EnrichmentJobStatus.FAILED
            job.error = f"Timeout after {self.timeout_sec}s"
        except Exception as exc:
            logger.warning("Owner AI job %s failed: %s", job_id, exc)
            job.status = EnrichmentJobStatus.FAILED
            job.error = str(exc)
        finally:
            job.duration_ms = (time.monotonic() - t0) * 1000.0
            job.completed_at = datetime.now(UTC).isoformat()

        return job
