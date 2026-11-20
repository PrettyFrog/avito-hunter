"""Tenant-scoped optimistic pause/resume through the SQLite writer."""
from __future__ import annotations

import inspect
from typing import Any

import aiosqlite

from hunter.storage.subscription_rebind import _seed_seen
from hunter.storage.subscriptions import (
    AccessDeniedError,
    NoActiveSlotCapacityError,
    StaleSubscriptionVersionError,
    SubscriptionActor,
    SubscriptionNotFoundError,
    SubscriptionRecord,
    SubscriptionRepository,
    _assign_query_tx,
)


async def _sync_assignment_on_toggle(
    conn: aiosqlite.Connection, qid: str, enabled: bool, auto_assign: bool, capacity: int
) -> None:
    if enabled:
        if auto_assign:
            row = await (await conn.execute(
                "SELECT COUNT(*) FROM assignments WHERE query_id = ? AND active = 1", (qid,)
            )).fetchone()
            if not row or int(row[0]) == 0:
                await _assign_query_tx(conn, qid, capacity)
    else:
        active = await (await conn.execute(
            "SELECT 1 FROM subscriptions WHERE query_id = ? AND enabled = 1 LIMIT 1", (qid,)
        )).fetchone()
        if active is None:
            await conn.execute(
                "UPDATE assignments SET active=0,updated_at=datetime('now') WHERE query_id=? AND active=1",
                (qid,),
            )


async def set_subscription_enabled(
    repo: SubscriptionRepository,
    actor: SubscriptionActor,
    sub_id: str,
    expected_version: int,
    enabled: bool,
    *,
    auto_assign: bool = True,
    max_queries_per_slot: int = 4,
    on_query_assigned: Any = None,
) -> SubscriptionRecord:
    if type(enabled) is not bool:
        raise ValueError("enabled must be boolean")

    target_qid: str | None = None

    async def tx(conn: aiosqlite.Connection) -> None:
        nonlocal target_qid
        row = await (await conn.execute(
            "SELECT user_id,query_id,version,enabled FROM subscriptions WHERE id=?", (sub_id,)
        )).fetchone()
        if row is None:
            raise SubscriptionNotFoundError("Subscription not found")
        if int(row[0]) != actor.user_id:
            raise AccessDeniedError("Subscription belongs to another user")
        if int(row[2]) != expected_version:
            raise StaleSubscriptionVersionError("Subscription version changed")
        if bool(row[3]) == enabled:
            return
        qid = str(row[1])
        target_qid = qid

        if enabled:
            await _sync_assignment_on_toggle(conn, qid, True, auto_assign, max_queries_per_slot)
            await _seed_seen(conn, sub_id, qid)
        await conn.execute(
            "UPDATE subscriptions SET enabled=?,version=version+1,updated_at=datetime('now') "
            "WHERE id=? AND user_id=? AND version=?",
            (int(enabled), sub_id, actor.user_id, expected_version),
        )
        if not enabled:
            await _sync_assignment_on_toggle(conn, qid, False, auto_assign, max_queries_per_slot)

    await repo.writer.submit(tx)
    if on_query_assigned and target_qid:
        refreshed = on_query_assigned(target_qid)
        if inspect.isawaitable(refreshed):
            await refreshed
    return await repo.get(SubscriptionActor(actor.user_id), sub_id)
