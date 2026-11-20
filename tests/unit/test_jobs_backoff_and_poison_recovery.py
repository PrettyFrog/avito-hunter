from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from hunter.core.db import Database
from hunter.core.models import now_utc


@pytest.mark.asyncio
async def test_fail_job_sets_next_run_at_backoff(tmp_path: Path) -> None:
    """Verifies that failing a job calculates exponential next_run_at backoff (15s * attempts)
    rather than immediately re-leasing it in the same millisecond.
    """
    db = Database(tmp_path / "test.db")
    await db.connect()

    job_id = await db.enqueue_job("card_fetch", "prod1", "key1", {"url": "https://avito.ru/1"})
    assert job_id is not None

    # Lease job for attempt 1
    job = await db.lease_job("card_fetch")
    assert job is not None
    assert job["id"] == job_id

    # Fail job (attempt 1)
    status = await db.fail_job(job_id, "TargetClosedError", lease_token=job["lease_token"])
    assert status == "queued"

    job_row = await db.get_job(job_id)
    assert job_row is not None
    assert job_row["status"] == "queued"
    assert job_row["attempts"] == 1
    assert job_row["next_run_at"] is not None

    next_dt = datetime.fromisoformat(job_row["next_run_at"])
    now = now_utc()
    # next_run_at should be roughly now + 15s (between 10s and 20s in the future)
    diff = (next_dt - now).total_seconds()
    assert 10 <= diff <= 20

    # Immediate lease_job must return None because next_run_at is in the future
    leased_again = await db.lease_job("card_fetch")
    assert leased_again is None


@pytest.mark.asyncio
async def test_fail_job_reaches_poison_after_max_attempts(tmp_path: Path) -> None:
    """Verifies that exceeding max_attempts sets status to poison and clears next_run_at."""
    db = Database(tmp_path / "test.db")
    await db.connect()

    job_id = await db.enqueue_job("card_fetch", "prod1", "key1", {"url": "https://avito.ru/1"})

    # Fail 3 times (max_attempts = 3)
    s1 = await db.fail_job(job_id, "err 1")
    assert s1 == "queued"
    s2 = await db.fail_job(job_id, "err 2")
    assert s2 == "queued"
    s3 = await db.fail_job(job_id, "err 3")
    assert s3 == "poison"

    job_row = await db.get_job(job_id)
    assert job_row["status"] == "poison"
    assert job_row["attempts"] == 3
    assert job_row["next_run_at"] is None
