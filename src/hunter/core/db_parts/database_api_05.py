from __future__ import annotations
import asyncio
from datetime import timedelta
from typing import Any
import aiosqlite
import sqlite3
from hunter.core.db_parts.database_get_poison_jobs_summary_47 import alerts_sent_in_past_hour_db, bump_product_cards_db, bump_shadow_cards_db, clear_pending_vision_db, get_due_pending_vision_db, get_poison_jobs_summary_db, get_recent_request_errors_db, get_unevaluated_stage0_listings_db, list_jobs_db, log_request_db, queue_for_digest_db, sync_product_profiles_db, today_product_cards_db, today_shadow_cards_db
from hunter.core.db_parts.database_get_queued_digest_items_50 import clear_digest_queue_db, get_queued_digest_items_db
from hunter.core.db_parts.database_recover_leases_44 import build_lease_job_query, complete_job_db, fail_job_db, get_job_db, recover_leases_db, try_lease_single_job_db
from hunter.core.db_parts.database_sync_products_41 import enqueue_job_db, get_pending_vision_db, set_pending_vision_db
from hunter.core.models import now_utc


class _DatabaseApi05:
        async def set_pending_vision(
            self, avito_id: str, watchlist_id: str, attempts: int, next_try_at: str
        ) -> None:
            await set_pending_vision_db(self.set_runtime, avito_id, watchlist_id, attempts, next_try_at)

        async def get_pending_vision(self, avito_id: str) -> dict | None:
            return await get_pending_vision_db(self.get_runtime, avito_id)

        async def enqueue_job(
            self,
            kind: str,
            product_id: str,
            dedupe_key: str,
            payload: dict | str,
            priority: int = 0,
            max_attempts: int = 3,
            next_run_at: str | None = None,
        ) -> int | None:
            async with self.write_lock:
                conn = await self.connect()
                return await enqueue_job_db(
                    conn,
                    kind,
                    product_id,
                    dedupe_key,
                    payload,
                    priority,
                    max_attempts,
                    next_run_at,
                )

        async def recover_leases(self, min_interval_sec: float = 0.0) -> int:
            recovered, ts = await recover_leases_db(self.connect, min_interval_sec, self._last_lease_recovery_ts)
            self._last_lease_recovery_ts = ts
            return recovered

        async def lease_job(
            self,
            kind: str | None = None,
            lease_duration_sec: int = 120,
            kinds: list[str] | None = None,
        ) -> dict | None:
            async with self.write_lock:
                conn = await self.connect()
                now = now_utc()
                now_str = now.isoformat()
                lease_until = (now + timedelta(seconds=lease_duration_sec)).isoformat()
                query, params = build_lease_job_query(now_str, kind, kinds)

                for attempt in range(3):
                    try:
                        await self.recover_leases()
                        return await try_lease_single_job_db(conn, query, params, lease_until, now_str)
                    except (sqlite3.OperationalError, aiosqlite.OperationalError) as db_err:
                        if "locked" in str(db_err).lower() and attempt < 2:
                            await asyncio.sleep(0.05 * (2 ** attempt))
                            continue
                        raise

        async def complete_job(
            self,
            job_id: int,
            payload: dict | str | None = None,
            lease_token: str | None = None,
        ) -> bool:
            async with self.write_lock:
                conn = await self.connect()
                return await complete_job_db(conn, job_id, payload, lease_token)

        async def fail_job(
            self,
            job_id: int,
            error: str,
            lease_token: str | None = None,
        ) -> str | None:
            async with self.write_lock:
                conn = await self.connect()
                return await fail_job_db(conn, job_id, error, lease_token)

        async def renew_job_lease(self, job_id: int, lease_token: str, extend_sec: int = 60) -> bool:
            async with self.write_lock:
                conn = await self.connect()
                now = now_utc()
                new_until = (now + timedelta(seconds=extend_sec)).isoformat()
                now_str = now.isoformat()
                cur = await conn.execute(
                    'UPDATE jobs SET lease_until = ?, updated_at = ? WHERE id = ? AND lease_'
                        "token = ? AND status = 'leased' AND julianday(lease_until) > julianday(?)",
                    (new_until, now_str, job_id, lease_token, now_str),
                )
                await conn.commit()
                return cur.rowcount > 0

        async def get_job(self, job_id: int) -> dict | None:
            conn = await self.connect()
            return await get_job_db(conn, job_id)

        async def get_poison_jobs_summary(self) -> tuple[int, list[str]]:
            conn = await self.connect()
            return await get_poison_jobs_summary_db(conn)

        async def list_jobs(self, status: str | None = None, limit: int = 50) -> list[dict]:
            conn = await self.connect()
            return await list_jobs_db(conn, status, limit)

        async def log_request(
            self,
            product_id: str | None,
            req_type: str,
            status: int | None = None,
            url: str | None = None,
            latency_ms: int | None = None,
            error: str | None = None,
        ) -> None:
            conn = await self.connect()
            await log_request_db(conn, product_id, req_type, status, url, latency_ms, error)

        async def get_recent_request_errors(self, since_hours: int = 24, limit: int = 150) -> dict[str, Any]:
            """Returns 24h error stats and event logs for admin monitoring."""
            conn = await self.connect()
            return await get_recent_request_errors_db(conn, since_hours, limit)

        async def get_unevaluated_stage0_listings(self, since_hours: int = 48) -> list[dict]:
            conn = await self.connect()
            return await get_unevaluated_stage0_listings_db(conn, since_hours)

        async def sync_product_profiles(self, profiles: list[Any]) -> None:
            conn = await self.connect()
            await sync_product_profiles_db(conn, profiles)

        async def clear_pending_vision(self, avito_id: str) -> None:
            conn = await self.connect()
            await clear_pending_vision_db(conn, avito_id)

        async def get_due_pending_vision(self, now_iso: str) -> list[dict]:
            conn = await self.connect()
            return await get_due_pending_vision_db(conn, now_iso)

        async def today_shadow_cards(self, feed_type: str | None = None) -> int:
            return await today_shadow_cards_db(self.get_runtime, feed_type)

        async def bump_shadow_cards(self, feed_type: str = "model") -> int:
            return await bump_shadow_cards_db(self.get_runtime, self.set_runtime, feed_type)

        async def today_product_cards(self, product_id: str) -> int:
            return await today_product_cards_db(self.get_runtime, product_id)

        async def bump_product_cards(self, product_id: str) -> int:
            return await bump_product_cards_db(self.get_runtime, self.set_runtime, product_id)

        async def alerts_sent_in_past_hour(self) -> int:
            conn = await self.connect()
            return await alerts_sent_in_past_hour_db(conn)

        async def queue_for_digest(self, event_data: dict) -> None:
            await queue_for_digest_db(self.set_runtime, event_data)

        async def get_queued_digest_items(self) -> list[dict]:
            conn = await self.connect()
            return await get_queued_digest_items_db(conn)

        async def clear_digest_queue(self, keys: list[str] | None = None) -> None:
            conn = await self.connect()
            await clear_digest_queue_db(conn, keys)
