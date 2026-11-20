"""Price watch worker for known listings with bounded due queue (S7 / T-508)."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import aiosqlite

from hunter.domain.observations import ObservationBatch, ObservedListing, ParseOutcome, PriceState
from hunter.storage.observation_tx import execute_observation_tx
from hunter.storage.writer import SQLiteWriterActor
from hunter.transport.price_fetch import PriceFetchAdapter


class PriceWatchWorker:
    """Bounded due queue worker checking price changes on known lots."""

    def __init__(
        self,
        writer: SQLiteWriterActor,
        fetcher: PriceFetchAdapter | None = None,
        *,
        enabled: bool = False,
        spare_capacity: bool | Callable[[], bool] = True,
        watch_interval_sec: int = 3600,
        defer_sec: int = 60,
        base_backoff_sec: int = 60,
        batch_size: int = 10,
        next_sequence: Callable[[], Any] | None = None,
    ) -> None:
        self.writer = writer
        self.fetcher = fetcher or PriceFetchAdapter()
        self.enabled = enabled
        self._spare_capacity = spare_capacity
        self.watch_interval_sec, self.defer_sec = watch_interval_sec, defer_sec
        self.base_backoff_sec, self.batch_size = base_backoff_sec, batch_size
        self.next_sequence = next_sequence
        self.requests_made = self.jobs_processed = self.jobs_deferred = 0

    def has_capacity(self) -> bool:
        return (
            self._spare_capacity() if callable(self._spare_capacity) else bool(self._spare_capacity)
        )

    async def get_metrics(self) -> dict[str, Any]:
        async def _tx(c: aiosqlite.Connection) -> dict[str, Any]:
            cur = await c.execute("SELECT status, count(*) FROM price_watch_jobs GROUP BY status")
            counts = {str(r[0]): int(r[1]) for r in await cur.fetchall()}
            cur2 = await c.execute(
                "SELECT min(next_run_at), max(updated_at) FROM price_watch_jobs "
                "WHERE status = 'pending'"
            )
            oldest_due, last_updated = await cur2.fetchone() or (None, None)
            return {
                "enabled": self.enabled,
                "requests_made": self.requests_made,
                "jobs_processed": self.jobs_processed,
                "jobs_deferred": self.jobs_deferred,
                "counts": counts,
                "oldest_due": oldest_due,
                "last_updated": last_updated,
                "extras_overhead": True,
            }

        return await self.writer.submit(_tx)

    async def release_leases(self, token: str | None = None) -> int:
        async def _tx(c: aiosqlite.Connection) -> int:
            sql = (
                "UPDATE price_watch_jobs SET status = 'pending', lease_token = NULL, "
                "lease_until = NULL WHERE status = 'leased'"
            )
            return (
                await c.execute(
                    sql + " AND lease_token = ?" if token else sql, (token,) if token else ()
                )
            ).rowcount

        return await self.writer.submit(_tx)

    async def _claim_due(self, token: str, now_str: str) -> list[dict[str, Any]]:
        lease_until = (
            datetime.fromisoformat(now_str.replace("Z", "+00:00")) + timedelta(seconds=30)
        ).isoformat()

        async def _tx(c: aiosqlite.Connection) -> list[dict[str, Any]]:
            sql = (
                "SELECT j.id, j.item_id, j.query_group_id, j.attempts, j.max_attempts, "
                "coalesce(l.url, 'https://www.avito.ru/' || j.item_id), coalesce(l.title, '') "
                "FROM price_watch_jobs j LEFT JOIN listings l ON l.avito_id = j.item_id "
                "WHERE (j.status = 'pending' AND j.next_run_at <= ?) "
                "OR (j.status = 'leased' AND j.lease_until < ?) "
                "ORDER BY j.next_run_at ASC LIMIT ?"
            )
            rows = await (await c.execute(sql, (now_str, now_str, self.batch_size))).fetchall()
            claimed = []
            for r in rows:
                jid, iid, qgid, att, matt, url, title = (
                    r[0],
                    r[1],
                    r[2],
                    int(r[3]),
                    int(r[4]),
                    r[5],
                    r[6],
                )
                if (
                    await c.execute(
                        "UPDATE price_watch_jobs SET status = 'leased', lease_token = ?, "
                        "lease_until = ?, updated_at = ? WHERE id = ?",
                        (token, lease_until, now_str, jid),
                    )
                ).rowcount > 0:
                    claimed.append(
                        {
                            "id": jid,
                            "lease_token": token,
                            "item_id": iid,
                            "query_group_id": qgid,
                            "attempts": att,
                            "max_attempts": matt,
                            "url": url,
                            "title": title,
                        }
                    )
            return claimed

        return await self.writer.submit(_tx)

    async def _process_job(self, job: dict[str, Any], now_str: str) -> None:
        sequence = await self.next_sequence() if self.next_sequence else 1
        res = await self.fetcher.fetch_card_price(job["url"])
        if res.status == "deferred":
            await self._defer_job(job, now_str, res.retry_after_sec)
            return
        self.requests_made += 1
        batch: ObservationBatch | None = None
        if res.status == "ok" and res.price is not None:
            listing = ObservedListing(
                job["item_id"],
                job["title"] or f"Item {job['item_id']}",
                res.price,
                PriceState.ACTIVE,
                job["url"],
            )
            batch = ObservationBatch(
                "price_watch",
                job["query_group_id"] or f"watch_{job['item_id']}",
                now_str,
                ParseOutcome.OK,
                (listing,),
            )
            st, att, err = "pending", 0, None
            next_run = (
                datetime.fromisoformat(now_str.replace("Z", "+00:00"))
                + timedelta(seconds=self.watch_interval_sec)
            ).isoformat()
        elif res.status == "closed":
            st, att, err, next_run = "retired", job["attempts"], res.error or "closed", now_str
        else:
            att = job["attempts"] + 1
            st, err = (
                "dead_letter" if att >= job["max_attempts"] else "pending",
                res.error or "unknown",
            )
            backoff = self.base_backoff_sec * (2 ** (att - 1))
            next_run = (
                now_str
                if st == "dead_letter"
                else (
                    datetime.fromisoformat(now_str.replace("Z", "+00:00"))
                    + timedelta(seconds=backoff)
                ).isoformat()
            )

        async def _update_tx(c: aiosqlite.Connection) -> None:
            if batch:
                await execute_observation_tx(c, batch, request_sequence=sequence)
            await c.execute(
                "UPDATE price_watch_jobs SET status = ?, lease_token = NULL, "
                "lease_until = NULL, attempts = ?, next_run_at = ?, last_error = ?, "
                "updated_at = ? WHERE id = ?",
                (st, att, next_run, err, now_str, job["id"]),
            )

        await self.writer.submit(_update_tx)
        self.jobs_processed += 1

    async def _defer_job(self, job: dict[str, Any], now_str: str, retry_after: float | None) -> None:
        deadline = (datetime.fromisoformat(now_str.replace("Z", "+00:00"))
                    + timedelta(seconds=max(self.defer_sec, retry_after or 0))).isoformat()
        async def update(conn):
            return (await conn.execute(
                "UPDATE price_watch_jobs SET status='pending',lease_token=NULL,lease_until=NULL,"
                "next_run_at=?,updated_at=? WHERE id=? AND status='leased' AND lease_token=?",
                (deadline, now_str, job["id"], job["lease_token"]),
            )).rowcount
        self.jobs_deferred += await self.writer.submit(update)

    async def poll_once(self, now_ts: str | None = None) -> int:
        if not self.enabled:
            return 0
        now_str = now_ts or datetime.now(UTC).isoformat()
        if not self.has_capacity():

            async def _defer(c: aiosqlite.Connection) -> int:
                return (
                    await c.execute(
                        "UPDATE price_watch_jobs SET next_run_at = datetime(?, "
                        "'+' || ? || ' seconds'), "
                        "updated_at = ? WHERE status = 'pending' AND next_run_at <= ?",
                        (now_str, self.defer_sec, now_str, now_str),
                    )
                ).rowcount

            self.jobs_deferred += await self.writer.submit(_defer)
            return 0
        claimed = await self._claim_due(uuid.uuid4().hex, now_str)
        for job in claimed:
            await self._process_job(job, now_str)
        return len(claimed)
