"""Database slice 44: Job queue lease recovery and execution leases."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta
import json
import sqlite3
import time
from typing import Any
import uuid
import aiosqlite
from loguru import logger

from hunter.core.models import now_utc


async def recover_leases_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    min_interval_sec: float = 0.0,
    last_recovery_ts: float = 0.0,
) -> tuple[int, float]:
    """Recover expired leased jobs back to queued, or mark as poison if attempts exceeded."""
    now_mono = time.monotonic()
    if min_interval_sec > 0 and (now_mono - last_recovery_ts) < min_interval_sec:
        return 0, last_recovery_ts
    conn = await connect()
    now_str = now_utc().isoformat()

    # Fast read-only check: avoid write transactions when there are no expired leased jobs
    cur_check = await conn.execute(
        "SELECT 1 FROM jobs WHERE status = 'leased' AND (lease_until IS NULL OR "
            'julianday(lease_until) <= julianday(?)) LIMIT 1',
        (now_str,),
    )
    if not await cur_check.fetchone():
        return 0, now_mono

    try:
        # 1. Mark expired leased jobs where attempts >= max_attempts as poison
        cur_poison = await conn.execute(
            """
            UPDATE jobs
            SET status = 'poison', lease_until = NULL, lease_token = NULL, last_error = 'Превышено время удержания задачи (timeout)', updated_at = ?
            WHERE status = 'leased' AND (lease_until IS NULL OR julianday(lease_until) <= julianday(?)) AND attempts >= max_attempts
            """,
            (now_str, now_str),
        )
        poisoned = cur_poison.rowcount

        # 2. Re-queue expired leased jobs with remaining attempts
        cur = await conn.execute(
            """
            UPDATE jobs
            SET status = 'queued', lease_until = NULL, lease_token = NULL, updated_at = ?
            WHERE status = 'leased' AND (lease_until IS NULL OR julianday(lease_until) <= julianday(?)) AND attempts < max_attempts
            """,
            (now_str, now_str),
        )
        recovered = cur.rowcount
        await conn.commit()
        if poisoned > 0:
            logger.warning(
                'Marked {} expired leased jobs with exceeded attempts as '
                'poison', poisoned)
        if recovered > 0:
            logger.info("Recovered {} expired/orphaned leased jobs back to queued", recovered)
        return recovered, time.monotonic()
    except Exception:
        try:
            await conn.rollback()
        except Exception:
            pass
        raise


def build_lease_job_query(
    now_str: str,
    kind: str | None = None,
    kinds: list[str] | None = None,
) -> tuple[str, tuple[Any, ...]]:
    """Build parameterized SELECT query for leasing the next queued job."""
    query = """
        SELECT id, kind, product_id, dedupe_key, payload, priority, status, attempts, max_attempts
        FROM jobs
        WHERE status = 'queued'
          AND (next_run_at IS NULL OR datetime(next_run_at) <= datetime(?))
    """
    params: list[Any] = [now_str]
    if kinds:
        placeholders = ", ".join("?" for _ in kinds)
        query += f" AND kind IN ({placeholders})"
        params.extend(kinds)
    elif kind:
        query += " AND kind = ?"
        params.append(kind)
    query += " ORDER BY priority DESC, id ASC LIMIT 1"
    return query, tuple(params)


async def try_lease_single_job_db(
    conn: aiosqlite.Connection,
    query: str,
    params: tuple[Any, ...],
    lease_until: str,
    now_str: str,
) -> dict[str, Any] | None:
    """Attempt to claim the next eligible queued job under transaction."""
    await conn.execute("BEGIN IMMEDIATE")
    try:
        cur = await conn.execute(query, params)
        row = await cur.fetchone()
        if not row:
            await conn.commit()
            return None

        job_id = row["id"]
        new_attempts = row["attempts"] + 1
        token = uuid.uuid4().hex
        await conn.execute(
            """
            UPDATE jobs
            SET status = 'leased', attempts = ?, lease_until = ?, lease_token = ?, updated_at = ?
            WHERE id = ?
            """,
            (new_attempts, lease_until, token, now_str, job_id),
        )
        await conn.commit()
        job_dict = dict(row)
        job_dict["status"] = "leased"
        job_dict["attempts"] = new_attempts
        job_dict["lease_until"] = lease_until
        job_dict["lease_token"] = token
        return job_dict
    except Exception:
        try:
            await conn.rollback()
        except Exception:
            pass
        raise


async def complete_job_db(
    conn: aiosqlite.Connection,
    job_id: int,
    payload: dict[str, Any] | str | None = None,
    lease_token: str | None = None,
) -> bool:
    """Mark a queued or leased job as completed."""
    now_str = now_utc().isoformat()
    for attempt in range(3):
        try:
            p_str = None
            if payload is not None:
                p_str = json.dumps(payload, ensure_ascii=False) if isinstance(payload, dict) else str(payload)

            sql = "UPDATE jobs SET status = 'done', lease_until = NULL, lease_token = NULL, "
            if p_str is not None:
                sql += "payload = ?, "
            sql += "updated_at = ? WHERE id = ?"
            params: list[Any] = []
            if p_str is not None:
                params.append(p_str)
            params.extend([now_str, job_id])
            if lease_token is not None:
                sql +=(
                    " AND status = 'leased' AND lease_token = ? AND julianday(lease_until) > "
                    'julianday(?)'
                )
                params.extend([lease_token, now_str])
            else:
                sql += " AND status = 'queued'"

            cur = await conn.execute(sql, tuple(params))
            await conn.commit()
            return cur.rowcount > 0
        except (sqlite3.OperationalError, aiosqlite.OperationalError) as db_err:
            try:
                await conn.rollback()
            except Exception:
                pass
            if "locked" in str(db_err).lower() and attempt < 2:
                await asyncio.sleep(0.05 * (2 ** attempt))
                continue
            raise


async def fail_job_db(
    conn: aiosqlite.Connection,
    job_id: int,
    error: str,
    lease_token: str | None = None,
) -> str | None:
    """Mark a job as failed, retryable queued or permanent poison."""
    now = now_utc()
    now_str = now.isoformat()
    for attempt in range(3):
        try:
            query = "SELECT attempts, max_attempts, status, lease_token FROM jobs WHERE id = ?"
            params: list[Any] = [job_id]
            if lease_token is not None:
                query += " AND status = 'leased' AND lease_token = ? AND julianday(lease_until) > julianday(?)"
                params.extend([lease_token, now_str])
            else:
                query += " AND status = 'queued'"
            cur = await conn.execute(query, tuple(params))
            row = await cur.fetchone()
            if not row and lease_token is not None:
                return None
            if not row:
                return None
            attempts = row["attempts"] if row else 0
            max_attempts = row["max_attempts"] if row else 3
            if row and row["status"] != "leased":
                attempts += 1

            if attempts >= max_attempts:
                new_status = "poison"
                next_run_at = None
            else:
                new_status = "queued"
                next_run_at = (now + timedelta(seconds=15 * attempts)).isoformat()

            upd_sql = (
                "UPDATE jobs SET status = ?, attempts = ?, last_error = ?, lease_until = NULL, lease_token = NULL, "
                "next_run_at = ?, updated_at = ? WHERE id = ?"
            )
            upd_params = [new_status, attempts, str(error)[:500], next_run_at, now_str, job_id]
            if lease_token is not None:
                upd_sql += " AND status = 'leased' AND lease_token = ? AND julianday(lease_until) > julianday(?)"
                upd_params.extend([lease_token, now_str])
            else:
                upd_sql += " AND status = 'queued'"

            cur_upd = await conn.execute(upd_sql, tuple(upd_params))
            await conn.commit()
            return new_status if cur_upd.rowcount > 0 else None
        except (sqlite3.OperationalError, aiosqlite.OperationalError) as db_err:
            try:
                await conn.rollback()
            except Exception:
                pass
            if "locked" in str(db_err).lower() and attempt < 2:
                await asyncio.sleep(0.05 * (2 ** attempt))
                continue
            raise


async def get_job_db(conn: aiosqlite.Connection, job_id: int) -> dict[str, Any] | None:
    """Fetch job by ID."""
    cur = await conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
    row = await cur.fetchone()
    return dict(row) if row else None


