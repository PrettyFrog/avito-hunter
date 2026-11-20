"""Tenant-scoped repository with optimistic versions and durable tombstones."""

from __future__ import annotations

from contextlib import suppress
import sqlite3
from dataclasses import dataclass

import aiosqlite

from hunter.storage.connection import StorageConnectionManager
from hunter.storage.writer import SQLiteWriterActor


class SubscriptionError(Exception):
    pass


class AccessDeniedError(SubscriptionError):
    pass


class SubscriptionNotFoundError(SubscriptionError):
    pass


class StaleSubscriptionVersionError(SubscriptionError):
    pass


class TombstonedSubscriptionError(SubscriptionError):
    pass


class NoActiveSlotCapacityError(SubscriptionError):
    pass


@dataclass(frozen=True, slots=True)
class SubscriptionActor:
    user_id: int
    is_owner: bool = False


@dataclass(frozen=True, slots=True)
class SubscriptionRecord:
    id: str
    user_id: int
    query_id: str
    max_price: int
    manual_stopwords: str
    enabled: bool
    version: int
    ai_enrichment_enabled: bool = False
    created_at: str = ""
    updated_at: str = ""
    name: str = ""
    manual_keywords: str = ""


def validate_subscription_name(name: str) -> str:
    clean = name.strip()
    if not clean or len(clean) > 100 or any(ord(char) < 32 for char in name):
        raise ValueError("Название должно содержать от 1 до 100 символов в одной строке")
    return clean


TOMB_SQL = (
    "CREATE TABLE IF NOT EXISTS subscription_tombstones "
    "(id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, query_id TEXT NOT NULL, "
    "tombstoned_at TEXT NOT NULL DEFAULT "
    "(datetime('now')))"
)


class SubscriptionRepository:
    def __init__(self, mgr: StorageConnectionManager, writer: SQLiteWriterActor) -> None:
        self.mgr = mgr
        self.writer = writer

    async def bind_telegram_chat(self, user_id: int, chat_id: int) -> None:
        if int(user_id) <= 0 or int(chat_id) <= 0:
            raise ValueError("Telegram user and chat ids must be positive")

        async def _tx(c: aiosqlite.Connection) -> None:
            await c.execute(
                "INSERT INTO channel_destinations (user_id, channel, address, verified, version) "
                "VALUES (?, 'telegram', ?, 1, 1) ON CONFLICT(user_id, channel) DO UPDATE SET "
                "address = excluded.address, verified = 1, "
                "version = channel_destinations.version + "
                "CASE WHEN channel_destinations.address != excluded.address "
                "OR channel_destinations.verified != 1 THEN 1 ELSE 0 END, "
                "updated_at = datetime('now')",
                (int(user_id), str(chat_id)),
            )

        await self.writer.submit(_tx)

    async def set_destination_verified(self, user_id: int, channel: str, verified: bool) -> bool:
        async def _tx(c: aiosqlite.Connection) -> bool:
            cur = await c.execute(
                "UPDATE channel_destinations SET verified = ?, version = version + 1, "
                "updated_at = datetime('now') WHERE user_id = ? AND channel = ?",
                (int(verified), int(user_id), channel),
            )
            return cur.rowcount > 0

        return await self.writer.submit(_tx)

    async def is_tombstoned(self, sub_id: str) -> bool:
        reader = await self.mgr.open_read_connection()
        try:
            cur = await reader.execute(
                "SELECT 1 FROM subscription_tombstones WHERE id = ?", (sub_id,)
            )
            return (await cur.fetchone()) is not None
        except (sqlite3.OperationalError, aiosqlite.OperationalError):
            return False
        finally:
            await self.mgr.close_reader(reader)

    async def create(
        self,
        actor: SubscriptionActor,
        sub_id: str,
        target_user: int,
        qid: str,
        pmax: int,
        words: str = "",
        url: str = "",
        *,
        auto_assign: bool = False,
        max_queries_per_slot: int = 4,
        name: str | None = None,
        keywords: str = "",
    ) -> SubscriptionRecord:
        if not actor.is_owner and actor.user_id != target_user:
            raise AccessDeniedError(f"Actor {actor.user_id} cannot create sub for {target_user}")
        if await self.is_tombstoned(sub_id):
            raise TombstonedSubscriptionError(f"Subscription {sub_id} is tombstoned")

        clean_name = validate_subscription_name(name) if name is not None else ""

        async def _tx(c: aiosqlite.Connection) -> SubscriptionRecord:
            await c.execute(
                "INSERT OR IGNORE INTO queries (id, canonical_url, bootstrap_state) "
                "VALUES (?, ?, 'pending')",
                (qid, url or f"https://avito.ru/q/{qid}"),
            )
            if auto_assign:
                await _assign_query_tx(c, qid, max_queries_per_slot)
            await c.execute(
                "INSERT INTO subscriptions "
                "(id, user_id, query_id, max_price, manual_stopwords, enabled, version, name, manual_keywords) "
                "VALUES (?, ?, ?, ?, ?, 1, 1, ?, ?)",
                (sub_id, target_user, qid, pmax, words, clean_name, keywords),
            )
            await c.execute(
                "INSERT OR IGNORE INTO subscription_seen "
                "(subscription_id, avito_id, baseline_epoch, last_revision, created_at) "
                "SELECT ?, avito_id, 1, revision, datetime('now') FROM listing_state "
                "WHERE avito_id IN (SELECT avito_id FROM query_membership WHERE query_id = ?) "
                "OR avito_id IN (SELECT avito_id FROM events WHERE origin = ?)",
                (sub_id, qid, qid),
            )
            return SubscriptionRecord(
                sub_id, target_user, qid, pmax, words, True, 1, name=clean_name, manual_keywords=keywords
            )

        return await self.writer.submit(_tx)

    async def get(self, actor: SubscriptionActor, sub_id: str) -> SubscriptionRecord:
        reader = await self.mgr.open_read_connection()
        try:
            cur = await reader.execute(
                "SELECT id, user_id, query_id, max_price, manual_stopwords, enabled, version, "
                "ai_enrichment_enabled, created_at, updated_at, name, COALESCE(manual_keywords, '') "
                "FROM subscriptions WHERE id = ?",
                (sub_id,),
            )
            r = await cur.fetchone()
            if not r:
                raise SubscriptionNotFoundError(f"Sub {sub_id} not found")
            rec = SubscriptionRecord(
                r[0],
                int(r[1]),
                r[2],
                int(r[3]),
                r[4],
                bool(r[5]),
                int(r[6]),
                bool(r[7]),
                str(r[8]),
                str(r[9]),
                str(r[10]),
                str(r[11]) if len(r) > 11 else "",
            )
            if not actor.is_owner and actor.user_id != rec.user_id:
                raise AccessDeniedError(f"Actor {actor.user_id} cannot access sub {rec.id}")
            return rec
        finally:
            await self.mgr.close_reader(reader)

    async def list_for_actor(self, actor: SubscriptionActor) -> list[SubscriptionRecord]:
        reader = await self.mgr.open_read_connection()
        try:
            sql = (
                "SELECT id, user_id, query_id, max_price, manual_stopwords, enabled, version, "
                "ai_enrichment_enabled, created_at, updated_at, name, COALESCE(manual_keywords, '') FROM subscriptions "
            )
            params = () if actor.is_owner else (actor.user_id,)
            if not actor.is_owner:
                sql += "WHERE user_id = ? "
            cur = await reader.execute(sql + "ORDER BY created_at DESC", params)
            return [
                SubscriptionRecord(
                    r[0],
                    int(r[1]),
                    r[2],
                    int(r[3]),
                    r[4],
                    bool(r[5]),
                    int(r[6]),
                    bool(r[7]),
                    str(r[8]),
                    str(r[9]),
                    str(r[10]),
                    str(r[11]) if len(r) > 11 else "",
                )
                for r in await cur.fetchall()
            ]
        finally:
            await self.mgr.close_reader(reader)

    async def update(
        self,
        actor: SubscriptionActor,
        sub_id: str,
        expected_version: int,
        max_price: int | None = None,
        words: str | None = None,
        name: str | None = None,
        keywords: str | None = None,
    ) -> SubscriptionRecord:
        await self.get(actor, sub_id)
        clean_name = validate_subscription_name(name) if name is not None else None

        async def _tx(c: aiosqlite.Connection) -> SubscriptionRecord:
            cur = await c.execute(
                "UPDATE subscriptions SET max_price = COALESCE(?, max_price), "
                "manual_stopwords = COALESCE(?, manual_stopwords), name = COALESCE(?, name), "
                "manual_keywords = COALESCE(?, manual_keywords), "
                "version = version + 1, updated_at = datetime('now') "
                "WHERE id = ? AND version = ?",
                (max_price, words, clean_name, keywords, sub_id, expected_version),
            )
            if cur.rowcount == 0:
                raise StaleSubscriptionVersionError(
                    f"Stale version {expected_version} for {sub_id}"
                )
            r = await (
                await c.execute(
                    "SELECT id, user_id, query_id, max_price, manual_stopwords, enabled, "
                    "version, name, COALESCE(manual_keywords, '') FROM subscriptions WHERE id = ?",
                    (sub_id,),
                )
            ).fetchone()
            return SubscriptionRecord(
                r[0], int(r[1]), r[2], int(r[3]), r[4], bool(r[5]), int(r[6]), name=r[7], manual_keywords=r[8] if len(r) > 8 else ""
            )

        return await self.writer.submit(_tx)

    async def delete(
        self, actor: SubscriptionActor, sub_id: str, expected_version: int | None = None
    ) -> None:
        async def _tx(c: aiosqlite.Connection) -> None:
            row = await (await c.execute(
                "SELECT user_id, query_id, version FROM subscriptions WHERE id=?", (sub_id,)
            )).fetchone()
            if row is None:
                raise SubscriptionNotFoundError("Subscription not found")
            if not actor.is_owner and int(row[0]) != actor.user_id:
                raise AccessDeniedError("Subscription belongs to another user")
            if expected_version is not None and int(row[2]) != expected_version:
                raise StaleSubscriptionVersionError("Subscription version changed")
            user_id, query_id = int(row[0]), str(row[1])
            await c.execute(TOMB_SQL)
            await c.execute("DELETE FROM subscriptions WHERE id = ?", (sub_id,))
            await c.execute(
                "INSERT OR REPLACE INTO subscription_tombstones "
                "(id, user_id, query_id) VALUES (?, ?, ?)",
                (sub_id, user_id, query_id),
            )
            await c.execute(
                "DELETE FROM deliveries WHERE user_id = ? AND status = 'pending' "
                "AND event_id IN (SELECT id FROM events WHERE origin = ?) "
                "AND NOT EXISTS (SELECT 1 FROM subscriptions WHERE user_id = ?)",
                (user_id, query_id, user_id),
            )
            await c.execute(
                "UPDATE assignments SET active=0,updated_at=datetime('now') "
                "WHERE query_id=? AND active=1 AND NOT EXISTS "
                "(SELECT 1 FROM subscriptions WHERE query_id=? AND enabled=1)",
                (query_id, query_id),
            )

        await self.writer.submit(_tx)


async def reconcile_stale_assignments(writer: SQLiteWriterActor) -> None:
    async def _tx(c: aiosqlite.Connection) -> None:
        with suppress(Exception):
            await c.execute(
                "UPDATE subscriptions SET enabled = 0 WHERE enabled = 1 "
                "AND user_id IN (SELECT id FROM users WHERE status = 'deleted')"
            )
        await _reconcile_stale_assignments_tx(c)

    await writer.submit(_tx)


async def _reconcile_stale_assignments_tx(c: aiosqlite.Connection, current_qid: str | None = None) -> None:
    has_status = False
    with suppress(Exception):
        cols = [r[1] for r in await (await c.execute("PRAGMA table_info(users)")).fetchall()]
        has_status = "status" in cols
    user_clause = "OR SUM(CASE WHEN s.user_id NOT IN (SELECT id FROM users WHERE status = 'deleted') THEN 1 ELSE 0 END) = 0" if has_status else ""
    qid_clause = "AND query_id != ?" if current_qid else ""
    params = (current_qid,) if current_qid else ()
    await c.execute(
        f"""UPDATE assignments SET active = 0, updated_at = datetime('now')
           WHERE active = 1 {qid_clause} AND query_id IN (
               SELECT s.query_id FROM subscriptions s
               GROUP BY s.query_id
               HAVING SUM(CASE WHEN s.enabled = 1 THEN 1 ELSE 0 END) = 0
                  {user_clause}
           )""",
        params,
    )


async def _assign_query_tx(c: aiosqlite.Connection, qid: str, capacity: int) -> None:
    if not 1 <= int(capacity) <= 4:
        raise ValueError("max_queries_per_slot must be between 1 and 4")
    await _reconcile_stale_assignments_tx(c, current_qid=qid)
    active = await (
        await c.execute(
            "SELECT COUNT(*) FROM assignments WHERE query_id = ? AND active = 1", (qid,)
        )
    ).fetchone()
    if int(active[0]) > 1:
        raise SubscriptionError(f"Query {qid} has multiple active slot assignments")
    if int(active[0]) == 1:
        return
    slots = await (
        await c.execute(
            """SELECT s.id, s.active_generation, COUNT(a.id) AS load
           FROM slots s LEFT JOIN assignments a ON a.slot_id = s.id AND a.active = 1
           WHERE lower(s.health_state) IN ('ready', 'warming', 'healthy', 'suspect')
             AND lower(s.role) = 'active'
           GROUP BY s.id ORDER BY (CASE WHEN lower(s.health_state) = 'suspect' THEN 1 ELSE 0 END), load, s.id"""
        )
    ).fetchall()
    for slot_id, generation, load in slots:
        if int(load) >= capacity:
            continue
        positions = await (
            await c.execute(
                "SELECT position FROM assignments WHERE slot_id = ? AND active = 1", (slot_id,)
            )
        ).fetchall()
        occupied = {int(row[0]) for row in positions}
        position = next((i for i in range(capacity) if i not in occupied), None)
        if position is None:
            continue
        new_generation = int(generation) + 1
        await c.execute(
            "UPDATE slots SET active_generation = ?, updated_at = datetime('now') WHERE id = ?",
            (new_generation, slot_id),
        )
        assigned = await c.execute(
            """INSERT INTO assignments
               (id, slot_id, query_id, position, generation, active, updated_at)
               VALUES (?, ?, ?, ?, ?, 1, datetime('now'))
               ON CONFLICT(slot_id, position) DO UPDATE SET
                   id = excluded.id, query_id = excluded.query_id,
                   generation = excluded.generation, active = 1, updated_at = excluded.updated_at
               WHERE assignments.active = 0""",
            (f"asgn_{slot_id}_{qid}_{new_generation}", slot_id, qid, position, new_generation),
        )
        if assigned.rowcount != 1:
            raise NoActiveSlotCapacityError("Slot position was concurrently occupied")
        return
    raise NoActiveSlotCapacityError("No active slot has capacity for this public search")
