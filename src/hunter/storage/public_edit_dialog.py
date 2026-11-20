"""Transactional, tenant-scoped editing dialogs for public searches."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

import aiosqlite

from hunter.storage.subscriptions import (
    AccessDeniedError,
    StaleSubscriptionVersionError,
    SubscriptionActor,
    SubscriptionNotFoundError,
    SubscriptionRepository,
    validate_subscription_name,
)


async def begin_edit(
    repo: SubscriptionRepository,
    actor: SubscriptionActor,
    chat_id: int,
    sub_id: str,
    version: int,
    field: Literal["name", "words", "keywords", "price", "url"],
) -> None:
    if field not in ("name", "words", "keywords", "price", "url"):
        raise ValueError("Unknown edit field")
    now = datetime.now(UTC)
    payload = json.dumps(
        {"sub_id": sub_id, "version": version, "field": field, "nonce": uuid.uuid4().hex}
    )

    async def tx(conn: aiosqlite.Connection) -> None:
        row = await (
            await conn.execute("SELECT user_id, version FROM subscriptions WHERE id=?", (sub_id,))
        ).fetchone()
        if not row:
            raise SubscriptionNotFoundError("Search not found")
        if int(row[0]) != actor.user_id:
            raise AccessDeniedError("Search is not owned by user")
        if int(row[1]) != version:
            raise StaleSubscriptionVersionError("Search changed")
        state = await (
            await conn.execute(
                "SELECT owner_user_id FROM pending_products WHERE chat_id=?", (chat_id,)
            )
        ).fetchone()
        if state and state[0] is not None and int(state[0]) != actor.user_id:
            raise AccessDeniedError("Dialog belongs to another user")
        await conn.execute(
            "INSERT INTO pending_products (chat_id,step,payload,created_at,updated_at, "
            "expires_at,owner_user_id) "
            "VALUES (?, 'public_search_edit', ?, ?, ?, ?, ?) ON CONFLICT(chat_id) DO UPDATE SET "
            "step=excluded.step,payload=excluded.payload,updated_at=excluded.updated_at, "
            "expires_at=excluded.expires_at,owner_user_id=excluded.owner_user_id",
            (
                chat_id,
                payload,
                now.isoformat(),
                now.isoformat(),
                (now + timedelta(hours=24)).isoformat(),
                actor.user_id,
            ),
        )

    await repo.writer.submit(tx)


async def finish_edit(
    repo: SubscriptionRepository,
    actor: SubscriptionActor,
    chat_id: int,
    text: str,
    expected_payload: dict[str, object] | None = None,
) -> None:
    async def tx(conn: aiosqlite.Connection) -> None:
        row = await (
            await conn.execute(
                "SELECT payload,expires_at,owner_user_id FROM pending_products "
                "WHERE chat_id=? AND step='public_search_edit'",
                (chat_id,),
            )
        ).fetchone()
        if not row or row[2] != actor.user_id or row[1] <= datetime.now(UTC).isoformat():
            raise AccessDeniedError("Dialog unavailable")
        state = json.loads(row[0])
        if expected_payload is not None and state != expected_payload:
            raise AccessDeniedError("Dialog changed")
        field = state.get("field")
        if field == "name":
            value = validate_subscription_name(text)
            col = "name"
        elif field in ("words", "keywords"):
            value = (
                ""
                if text.strip() == "-"
                else ",".join(word.strip().lower() for word in text.split(",") if word.strip())
            )
            col = "manual_stopwords" if field == "words" else "manual_keywords"
        else:
            raise ValueError("Unknown edit field")
        sql = f"UPDATE subscriptions SET {col}=?,version=version+1,updated_at=datetime('now') "
        cur = await conn.execute(
            sql + "WHERE id=? AND user_id=? AND version=?",
            (value, state["sub_id"], actor.user_id, state["version"]),
        )
        if cur.rowcount != 1:
            raise StaleSubscriptionVersionError("Search changed")
        await conn.execute(
            "DELETE FROM pending_products WHERE chat_id=? AND owner_user_id=? "
            "AND step='public_search_edit'",
            (chat_id, actor.user_id),
        )

    await repo.writer.submit(tx)
