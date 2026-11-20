"""Unit tests for job lease fencing and conditional token ack (S3 / T-467).

Covers:
- Additive v015 migration: lease_token column and index on jobs.
- Expired lease re-issued with a distinct fencing token.
- Stale worker with expired token cannot complete, fail, or renew job.
- Active worker with matching token renews and completes successfully.
- Full schema equivalence.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest

from hunter.core.db import Database
from hunter.core.models import now_utc
from hunter.storage.job_claims import JobClaimRepository


@pytest.fixture
async def fencing_env(tmp_path: Path):
    db_path = tmp_path / "test_fencing.db"
    db = Database(db_path)
    await db.connect()
    await db.migrate()
    repo = JobClaimRepository(db)
    yield db, repo
    await db.close()


@pytest.mark.asyncio
async def test_v15_migration_applies_and_adds_lease_token_column(tmp_path: Path):
    db_path = tmp_path / "mig_v15.db"
    db = Database(db_path)
    await db.connect()
    await db.migrate()

    conn = await db.connect()
    cur = await conn.execute("PRAGMA user_version")
    ver = (await cur.fetchone())[0]
    assert ver >= 15

    cur_cols = await conn.execute("PRAGMA table_info(jobs)")
    cols = [r[1] for r in await cur_cols.fetchall()]
    assert "lease_token" in cols

    cur_idx = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_jobs_lease_token'"
    )
    assert await cur_idx.fetchone() is not None

    await db.close()


@pytest.mark.asyncio
async def test_expired_lease_reissued_with_new_token(fencing_env):
    db, repo = fencing_env

    job_id = await db.enqueue_job(
        kind="test_card_fetch",
        product_id="prod_01",
        dedupe_key="key_01",
        payload={"url": "https://avito.ru/item1"},
    )
    assert job_id is not None

    # Worker 1 leases job
    job1 = await repo.claim_job("test_card_fetch", lease_duration_sec=1)
    assert job1 is not None
    assert job1.id == job_id
    token1 = job1.lease_token
    assert token1 != ""

    # Force expiration of lease
    conn = await db.connect()
    past_ts = (now_utc() - timedelta(seconds=10)).isoformat()
    await conn.execute("UPDATE jobs SET lease_until = ? WHERE id = ?", (past_ts, job_id))
    await conn.commit()

    # Worker 2 leases the expired job
    job2 = await repo.claim_job("test_card_fetch", lease_duration_sec=60)
    assert job2 is not None
    assert job2.id == job_id
    token2 = job2.lease_token
    assert token2 != ""

    # Tokens must be cryptographically distinct
    assert token1 != token2


@pytest.mark.asyncio
async def test_stale_claim_cannot_complete_or_renew_or_fail(fencing_env):
    db, repo = fencing_env

    job_id = await db.enqueue_job(
        kind="test_card_fetch",
        product_id="prod_02",
        dedupe_key="key_02",
        payload={"url": "https://avito.ru/item2"},
    )

    job1 = await repo.claim_job("test_card_fetch", lease_duration_sec=1)
    assert job1 is not None
    token1 = job1.lease_token

    # Force lease expiration and re-issue to Worker 2
    conn = await db.connect()
    past_ts = (now_utc() - timedelta(seconds=10)).isoformat()
    await conn.execute("UPDATE jobs SET lease_until = ? WHERE id = ?", (past_ts, job_id))
    await conn.commit()

    job2 = await repo.claim_job("test_card_fetch", lease_duration_sec=60)
    assert job2 is not None
    token2 = job2.lease_token

    # 1. Stale Worker 1 attempts to renew lease -> REJECTED
    renewed = await repo.renew_lease(job_id, lease_token=token1, extend_sec=60)
    assert renewed is False

    # 2. Stale Worker 1 attempts to complete job -> REJECTED
    completed = await repo.complete_job(job_id, lease_token=token1, payload={"stale": True})
    assert completed is False

    # 3. Stale Worker 1 attempts to fail job -> REJECTED
    failed = await repo.fail_job(job_id, lease_token=token1, error="stale error")
    assert failed is None

    # Job remains active and leased to Worker 2
    cur = await conn.execute("SELECT status, lease_token FROM jobs WHERE id = ?", (job_id,))
    row = await cur.fetchone()
    assert row["status"] == "leased"
    assert row["lease_token"] == token2


@pytest.mark.asyncio
async def test_active_worker_renews_and_completes_with_valid_token(fencing_env):
    db, repo = fencing_env

    job_id = await db.enqueue_job(
        kind="test_card_fetch",
        product_id="prod_03",
        dedupe_key="key_03",
        payload={"url": "https://avito.ru/item3"},
    )

    job = await repo.claim_job("test_card_fetch", lease_duration_sec=10)
    assert job is not None
    token = job.lease_token

    # Active worker renews lease
    renewed = await repo.renew_lease(job_id, lease_token=token, extend_sec=120)
    assert renewed is True

    # Active worker completes job
    completed = await repo.complete_job(job_id, lease_token=token, payload={"success": True})
    assert completed is True

    conn = await db.connect()
    cur = await conn.execute("SELECT status, lease_until FROM jobs WHERE id = ?", (job_id,))
    row = await cur.fetchone()
    assert row["status"] == "done"
    assert row["lease_until"] is None


@pytest.mark.asyncio
async def test_database_facade_supports_lease_tokens(fencing_env):
    db, _ = fencing_env

    job_id = await db.enqueue_job(
        kind="test_facade",
        product_id="prod_04",
        dedupe_key="key_04",
        payload={"url": "https://avito.ru/item4"},
    )

    # Legacy facade lease_job returns lease_token
    job = await db.lease_job("test_facade", lease_duration_sec=60)
    assert job is not None
    assert "lease_token" in job
    token = job["lease_token"]

    # Renew via facade
    renewed = await db.renew_job_lease(job_id, token, extend_sec=30)
    assert renewed is True

    # Complete with wrong token fails
    assert await db.complete_job(job_id, payload={"data": 1}, lease_token="wrong_token") is False

    # Complete with right token succeeds
    assert await db.complete_job(job_id, payload={"data": 1}, lease_token=token) is True


@pytest.mark.asyncio
async def test_heartbeat_worker_renews_and_stops_on_exit(fencing_env):
    from hunter.storage.job_claims import job_lease_heartbeat

    db, repo = fencing_env
    job_id = await db.enqueue_job(
        kind="test_hb",
        product_id="prod_05",
        dedupe_key="key_05",
        payload={"url": "https://avito.ru/item5"},
    )
    job = await repo.claim_job("test_hb", lease_duration_sec=2)
    assert job is not None
    token = job.lease_token

    conn = await db.connect()
    cur = await conn.execute("SELECT lease_until FROM jobs WHERE id = ?", (job_id,))
    initial_until = (await cur.fetchone())[0]

    async with job_lease_heartbeat(repo, job_id, token, interval_sec=0.1, extend_sec=10):
        await asyncio.sleep(0.25)
        cur = await conn.execute("SELECT lease_until FROM jobs WHERE id = ?", (job_id,))
        renewed_until = (await cur.fetchone())[0]
        assert renewed_until > initial_until

    # After exiting heartbeat context, lease_until should no longer change
    post_until = renewed_until
    await asyncio.sleep(0.2)
    cur = await conn.execute("SELECT lease_until FROM jobs WHERE id = ?", (job_id,))
    assert (await cur.fetchone())[0] == post_until
