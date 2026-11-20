"""Persisted query groups and membership repository (S7 / T-504)."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import aiosqlite

from hunter.domain.query_compatibility import compute_filter_fingerprint
from hunter.storage.connection import StorageConnectionManager
from hunter.storage.writer import SQLiteWriterActor


class QueryGroupError(Exception):
    pass


class AccessDeniedError(QueryGroupError):
    pass


class QueryGroupNotFoundError(QueryGroupError):
    pass


@dataclass(frozen=True, slots=True)
class QueryGroupRecord:
    id: str
    canonical_url: str
    filter_fingerprint: str
    max_price: int
    generation: int
    version: int
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class GroupMemberRecord:
    id: str
    group_id: str
    subscription_id: str
    user_id: int
    personal_max_price: int
    stopwords: tuple[str, ...]
    generation: int
    created_at: str
    updated_at: str


class QueryGroupRepository:
    """Manages persisted query groups, memberships, and tenant-scoped lifecycle."""

    def __init__(self, mgr: StorageConnectionManager, writer: SQLiteWriterActor) -> None:
        self.mgr = mgr
        self.writer = writer

    async def get_group(self, group_id: str) -> QueryGroupRecord | None:
        c = await self.mgr.open_read_connection()
        try:
            cur = await c.execute(
                "SELECT id, canonical_url, filter_fingerprint, max_price, generation, "
                "version, created_at, updated_at FROM query_groups WHERE id = ?",
                (group_id,),
            )
            r = await cur.fetchone()
            return (
                QueryGroupRecord(r[0], r[1], r[2], int(r[3]), int(r[4]), int(r[5]), r[6], r[7])
                if r
                else None
            )
        finally:
            await self.mgr.close_reader(c)

    async def get_members(self, group_id: str) -> list[GroupMemberRecord]:
        c = await self.mgr.open_read_connection()
        try:
            cur = await c.execute(
                "SELECT id, group_id, subscription_id, user_id, personal_max_price, "
                "stopwords, generation, created_at, updated_at FROM group_members "
                "WHERE group_id = ?",
                (group_id,),
            )
            rows = await cur.fetchall()
            return [
                GroupMemberRecord(
                    r[0],
                    r[1],
                    r[2],
                    int(r[3]),
                    int(r[4]),
                    tuple(json.loads(r[5])),
                    int(r[6]),
                    r[7],
                    r[8],
                )
                for r in rows
            ]
        finally:
            await self.mgr.close_reader(c)

    async def _resolve_group_id(
        self,
        c: aiosqlite.Connection,
        canon: str,
        fp: str,
        max_price: int,
        allow_compat: bool,
        mergeable: bool,
        now_ts: str,
    ) -> str:
        cur = await c.execute(
            "SELECT id FROM query_groups WHERE canonical_url = ? LIMIT 1", (canon,)
        )
        row = await cur.fetchone()
        if row:
            return str(row[0])
        if allow_compat and mergeable:
            cur = await c.execute(
                "SELECT id FROM query_groups WHERE filter_fingerprint = ? "
                "AND max_price >= ? ORDER BY max_price ASC LIMIT 1",
                (fp, max_price),
            )
            row = await cur.fetchone()
            if row:
                return str(row[0])
        gid = f"qg_{hashlib.sha256(canon.encode()).hexdigest()[:16]}"
        await c.execute(
            "INSERT INTO query_groups (id, canonical_url, filter_fingerprint, "
            "max_price, generation, version, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 1, 1, ?, ?)",
            (gid, canon, fp, max_price, now_ts, now_ts),
        )
        return gid

    async def _insert_member_and_baseline(
        self,
        c: aiosqlite.Connection,
        gid: str,
        canon: str,
        sub_id: str,
        uid: int,
        max_p: int,
        sw: str,
        ts: str,
    ) -> tuple[str, bool]:
        cur = await c.execute(
            "SELECT id FROM group_members WHERE group_id = ? AND subscription_id = ?",
            (gid, sub_id),
        )
        m_row = await cur.fetchone()
        if m_row:
            return str(m_row[0]), False
        mid = f"mem_{uuid.uuid4().hex[:12]}"
        await c.execute(
            "INSERT OR IGNORE INTO users (id, telegram_user_id, created_at) VALUES (?, ?, ?)",
            (uid, uid, ts),
        )
        await c.execute(
            "INSERT OR IGNORE INTO queries "
            "(id, canonical_url, bootstrap_state) VALUES (?, ?, 'pending')",
            (gid, canon),
        )
        await c.execute(
            "INSERT OR IGNORE INTO subscriptions (id, user_id, query_id, max_price, "
            "manual_stopwords, enabled, version) VALUES (?, ?, ?, ?, ?, 1, 1)",
            (sub_id, uid, gid, max_p, sw),
        )
        await c.execute(
            "INSERT INTO group_members (id, group_id, subscription_id, user_id, "
            "personal_max_price, stopwords, generation, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (mid, gid, sub_id, uid, max_p, sw, ts, ts),
        )
        await c.execute(
            "INSERT OR IGNORE INTO subscription_seen "
            "(subscription_id, avito_id, baseline_epoch, last_revision, created_at) "
            "SELECT ?, avito_id, 1, revision, ? FROM listing_state "
            "WHERE avito_id IN (SELECT avito_id FROM query_membership "
            "WHERE query_id = ? OR query_id = ? OR query_id IN "
            "(SELECT query_id FROM subscriptions WHERE id = ?)) "
            "OR avito_id IN (SELECT avito_id FROM events "
            "WHERE origin = ? OR origin = ? OR origin IN "
            "(SELECT query_id FROM subscriptions WHERE id = ?))",
            (sub_id, ts, gid, canon, sub_id, gid, canon, sub_id),
        )
        return mid, True

    async def attach(
        self,
        subscription_id: str,
        user_id: int,
        search_url: str,
        max_price: int,
        stopwords: tuple[str, ...] = (),
        canonical_url: str = "",
        allow_compatible_price_sharing: bool = True,
    ) -> tuple[QueryGroupRecord, GroupMemberRecord, bool]:
        canon, fp, now_ts, sw_json = (
            canonical_url or search_url,
            compute_filter_fingerprint(search_url, max_price)[0],
            datetime.now(UTC).isoformat(),
            json.dumps(list(stopwords), ensure_ascii=False),
        )
        _, mergeable, _ = compute_filter_fingerprint(search_url, max_price)

        async def _tx(c: aiosqlite.Connection) -> tuple[str, str, bool]:
            gid = await self._resolve_group_id(
                c, canon, fp, max_price, allow_compatible_price_sharing, mergeable, now_ts
            )
            mid, created = await self._insert_member_and_baseline(
                c, gid, canon, subscription_id, user_id, max_price, sw_json, now_ts
            )
            return gid, mid, created

        gid, mid, created = await self.writer.submit(_tx)
        grp = await self.get_group(gid)
        members = await self.get_members(gid)
        return grp, next(m for m in members if m.id == mid), created

    async def detach(self, group_id: str, subscription_id: str, user_id: int) -> bool:
        async def _tx(c: aiosqlite.Connection) -> bool:
            cur = await c.execute(
                "SELECT user_id FROM group_members WHERE group_id = ? AND subscription_id = ?",
                (group_id, subscription_id),
            )
            row = await cur.fetchone()
            if not row:
                return False
            if int(row[0]) != user_id:
                raise AccessDeniedError(
                    f"User {user_id} cannot detach subscription owned by {row[0]}"
                )
            await c.execute(
                "DELETE FROM group_members WHERE group_id = ? AND subscription_id = ?",
                (group_id, subscription_id),
            )
            rem = await (
                await c.execute(
                    "SELECT count(*) FROM group_members WHERE group_id = ?", (group_id,)
                )
            ).fetchone()
            if rem and rem[0] == 0:
                await c.execute("DELETE FROM query_groups WHERE id = ?", (group_id,))
            return True

        return await self.writer.submit(_tx)

    async def widen_ceiling(
        self,
        group_id: str,
        new_ceiling: int,
        new_canonical_url: str,
        transition_capacity: int = 0,
    ) -> tuple[QueryGroupRecord | None, bool, str]:
        grp = await self.get_group(group_id)
        if not grp:
            return None, False, "Group not found"
        if new_ceiling <= grp.max_price:
            return grp, False, "Ceiling already covers required price"
        if transition_capacity <= 0:
            return grp, False, "waiting: no transition capacity allocated for query overlap"

        now_ts = datetime.now(UTC).isoformat()
        old_ceiling = grp.max_price

        async def _tx(c: aiosqlite.Connection) -> None:
            await c.execute(
                "UPDATE query_groups SET max_price = ?, canonical_url = ?, "
                "generation = generation + 1, version = version + 1, "
                "updated_at = ? WHERE id = ?",
                (new_ceiling, new_canonical_url, now_ts, group_id),
            )
            q_cur = await c.execute(
                "SELECT id FROM queries WHERE canonical_url = ?", (new_canonical_url,)
            )
            if not await q_cur.fetchone():
                await c.execute(
                    "UPDATE queries SET canonical_url = ? WHERE id = ?",
                    (new_canonical_url, group_id),
                )
            await c.execute(
                "INSERT OR IGNORE INTO subscription_seen "
                "(subscription_id, avito_id, baseline_epoch, last_revision, created_at) "
                "SELECT m.subscription_id, ls.avito_id, 1, ls.revision, "
                "? FROM group_members m "
                "JOIN listing_state ls ON ls.known_price > ? "
                "AND ls.known_price <= ? WHERE m.group_id = ?",
                (now_ts, old_ceiling, new_ceiling, group_id),
            )

        await self.writer.submit(_tx)
        return await self.get_group(group_id), True, "OK"

    async def shrink_ceiling(
        self,
        group_id: str,
        new_canonical_url: str,
    ) -> tuple[QueryGroupRecord | None, bool]:
        grp = await self.get_group(group_id)
        if not grp:
            return None, False
        now_ts = datetime.now(UTC).isoformat()

        async def _tx(c: aiosqlite.Connection) -> bool:
            cur = await c.execute(
                "SELECT max(personal_max_price) FROM group_members WHERE group_id = ?", (group_id,)
            )
            row = await cur.fetchone()
            new_max = int(row[0]) if row and row[0] is not None else grp.max_price
            if new_max >= grp.max_price:
                return False
            await c.execute(
                "UPDATE query_groups SET max_price = ?, canonical_url = ?, "
                "generation = generation + 1, version = version + 1, "
                "updated_at = ? WHERE id = ?",
                (new_max, new_canonical_url, now_ts, group_id),
            )
            q_cur = await c.execute(
                "SELECT id FROM queries WHERE canonical_url = ?", (new_canonical_url,)
            )
            if not await q_cur.fetchone():
                await c.execute(
                    "UPDATE queries SET canonical_url = ? WHERE id = ?",
                    (new_canonical_url, group_id),
                )
            return True

        changed = await self.writer.submit(_tx)
        return await self.get_group(group_id), changed
