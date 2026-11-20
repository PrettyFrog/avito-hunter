"""Atomically finish an interactive physical-query edit."""

from __future__ import annotations

import inspect
import json
import logging
from datetime import UTC, datetime
from typing import Any

import aiosqlite

from hunter.application.subscription_rebind import change_query_price
from hunter.application.subscriptions import SubscriptionService, compute_query_id
from hunter.domain.public_price import parse_public_price
from hunter.domain.query_url import validate_and_canonicalize_query_url
from hunter.storage.subscription_rebind import rebind_in_transaction
from hunter.storage.subscriptions import AccessDeniedError, SubscriptionActor, SubscriptionRecord

logger = logging.getLogger(__name__)


async def finish_query_edit(
    service: SubscriptionService,
    actor: SubscriptionActor,
    chat_id: int,
    value: str,
    expected_payload: dict[str, object] | None = None,
) -> tuple[SubscriptionRecord, bool]:
    repo = service.repo
    payload, state, rec, price, url = await _prepare(service, actor, chat_id, value)
    if expected_payload is not None and state != expected_payload:
        raise AccessDeniedError("Dialog changed")

    async def tx(conn: aiosqlite.Connection) -> tuple[str, str]:
        row = await (
            await conn.execute(
                "SELECT 1 FROM pending_products WHERE chat_id=? AND owner_user_id=? "
                "AND step='public_search_edit' AND payload=? AND expires_at>?",
                (chat_id, actor.user_id, payload, datetime.now(UTC).isoformat()),
            )
        ).fetchone()
        if not row:
            raise AccessDeniedError("Dialog changed")
        old, new = await rebind_in_transaction(
            conn,
            SubscriptionActor(actor.user_id),
            rec.id,
            state["version"],
            compute_query_id(url),
            url,
            price,
            service.auto_assign,
            service.max_queries_per_slot,
        )
        await conn.execute("DELETE FROM pending_products WHERE chat_id=?", (chat_id,))
        return old, new

    old, new = await repo.writer.submit(tx)
    refreshed = await _refresh(service, old, new)
    return await repo.get(SubscriptionActor(actor.user_id), rec.id), refreshed


async def _prepare(
    service: SubscriptionService, actor: SubscriptionActor, chat_id: int, value: str
) -> tuple[str, dict[str, Any], SubscriptionRecord, int, str]:
    repo = service.repo
    reader = await repo.mgr.open_read_connection()
    try:
        row = await (
            await reader.execute(
                "SELECT payload, expires_at, owner_user_id FROM pending_products "
                "WHERE chat_id=? AND step='public_search_edit'",
                (chat_id,),
            )
        ).fetchone()
        if not row or row[2] != actor.user_id or row[1] <= datetime.now(UTC).isoformat():
            raise AccessDeniedError("Dialog unavailable")
        payload = str(row[0])
        state = json.loads(payload)
        rec = await repo.get(SubscriptionActor(actor.user_id), state["sub_id"])
        old_url = await (
            await reader.execute("SELECT canonical_url FROM queries WHERE id=?", (rec.query_id,))
        ).fetchone()
        if not old_url:
            raise ValueError("Query unavailable")
        if state["field"] == "price":
            price = parse_public_price(value)
            url = change_query_price(str(old_url[0]), price)
        elif state["field"] == "url":
            price = rec.max_price
            url = validate_and_canonicalize_query_url(value, price)
        else:
            raise ValueError("Not a query edit")
    finally:
        await repo.mgr.close_reader(reader)

    return payload, state, rec, price, url


async def _refresh(service: SubscriptionService, old: str, new: str) -> bool:
    refreshed = True
    if service.auto_assign and service.on_query_assigned:
        for qid in dict.fromkeys((old, new)):
            try:
                result = service.on_query_assigned(qid)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.error("Worker refresh failed after query edit")
                refreshed = False
    return refreshed
