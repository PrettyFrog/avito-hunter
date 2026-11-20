"""A lease token only authorizes a live, currently leased job."""

from datetime import timedelta
from pathlib import Path

import pytest

from hunter.core.db import Database
from hunter.core.models import now_utc
from hunter.storage.job_claims import JobClaimRepository


@pytest.mark.asyncio
@pytest.mark.parametrize("api", ["facade", "repository"])
async def test_expired_and_terminal_tokens_cannot_change_job(tmp_path: Path, api: str) -> None:
    db = Database(tmp_path / f"{api}.db")
    await db.connect()
    try:
        repo = JobClaimRepository(db)
        job_id = await db.enqueue_job("fencing", "product", api, {})
        if api == "facade":
            first = await db.lease_job("fencing")
            token = first["lease_token"]
        else:
            first = await repo.claim_job("fencing")
            token = first.lease_token

        conn = await db.connect()
        past = (now_utc() - timedelta(minutes=1)).isoformat()
        await conn.execute("UPDATE jobs SET lease_until = ? WHERE id = ?", (past, job_id))
        await conn.commit()

        if api == "facade":
            assert await db.complete_job(job_id, lease_token=token) is False
            assert await db.fail_job(job_id, "late", lease_token=token) is None
            assert await db.renew_job_lease(job_id, token) is False
        else:
            assert await repo.complete_job(job_id, token) is False
            assert await repo.fail_job(job_id, token, "late") is None
            assert await repo.renew_lease(job_id, token) is False

        assert await db.recover_leases() == 1
        row = await (await conn.execute(
            "SELECT status, lease_token FROM jobs WHERE id = ?", (job_id,)
        )).fetchone()
        assert row["status"] == "queued" and row["lease_token"] is None

        if api == "facade":
            second = await db.lease_job("fencing")
            new_token = second["lease_token"]
            assert await db.complete_job(job_id, lease_token=new_token) is True
            assert await db.fail_job(job_id, "late", lease_token=new_token) is None
        else:
            second = await repo.claim_job("fencing")
            new_token = second.lease_token
            assert await repo.complete_job(job_id, new_token) is True
            assert await repo.fail_job(job_id, new_token, "late") is None

        row = await (await conn.execute(
            "SELECT status, lease_token FROM jobs WHERE id = ?", (job_id,)
        )).fetchone()
        assert row["status"] == "done" and row["lease_token"] is None
    finally:
        await db.close()
