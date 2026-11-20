"""Atomically move a subscription between physical queries."""
from typing import Any

from hunter.storage.subscriptions import (
    AccessDeniedError,
    StaleSubscriptionVersionError,
    SubscriptionError,
    SubscriptionNotFoundError,
    _assign_query_tx,
)


async def _seed_seen(conn: Any, sub_id: str, query_id: str) -> None:
    await conn.execute(
        "INSERT INTO subscription_seen "
        "(subscription_id, avito_id, baseline_epoch, last_revision, created_at) "
        "SELECT ?, ls.avito_id, 1, ls.revision, datetime('now') FROM listing_state ls "
        "JOIN query_membership qm ON qm.avito_id = ls.avito_id WHERE qm.query_id = ? "
        "ON CONFLICT(subscription_id, avito_id) DO UPDATE SET "
        "last_revision = max(subscription_seen.last_revision, excluded.last_revision), "
        "baseline_epoch = 1", (sub_id, query_id),
    )


async def _prepare_target(conn: Any, qid: str, url: str) -> None:
    await conn.execute(
        "INSERT OR IGNORE INTO queries (id, canonical_url, bootstrap_state) "
        "VALUES (?, ?, 'pending')",
        (qid, url),
    )
    row = await (
        await conn.execute("SELECT canonical_url FROM queries WHERE id = ?", (qid,))
    ).fetchone()
    if not row or row[0] != url:
        raise SubscriptionError("Query identifier does not match canonical URL")


async def rebind_in_transaction(
    conn: Any, actor: Any, sub_id: str, expected_version: int, qid: str, url: str,
    price: int, auto_assign: bool, capacity: int, words: str | None = None,
) -> tuple[str, str]:
    if type(price) is not int or not 0 < price <= 9223372036854775807:
        raise ValueError("Price must be a positive SQLite integer")
    row = await (await conn.execute(
        "SELECT user_id, query_id, version FROM subscriptions WHERE id = ?", (sub_id,)
    )).fetchone()
    if not row:
        raise SubscriptionNotFoundError("Subscription not found")
    if not actor.is_owner and int(row[0]) != actor.user_id:
        raise AccessDeniedError("Subscription belongs to another user")
    old = str(row[1])
    if int(row[2]) != expected_version:
        raise StaleSubscriptionVersionError("Subscription version changed")
    group = await (await conn.execute(
        "SELECT 1 FROM group_members WHERE subscription_id = ? LIMIT 1", (sub_id,)
    )).fetchone()
    if group:
        raise SubscriptionError("Grouped subscription cannot be changed safely yet")
    await _prepare_target(conn, qid, url)
    await conn.execute(
        "UPDATE subscriptions SET query_id = ?, max_price = ?, "
        "manual_stopwords = COALESCE(?, manual_stopwords), version = version + 1, "
        "updated_at = datetime('now') WHERE id = ? AND version = ?",
        (qid, price, words, sub_id, expected_version),
    )
    if old != qid:
        await conn.execute(
            "UPDATE assignments SET active = 0, updated_at = datetime('now') "
            "WHERE query_id = ? AND active = 1 AND NOT EXISTS "
            "(SELECT 1 FROM subscriptions WHERE query_id = ? AND enabled = 1)", (old, old),
        )
        sub_row = await (await conn.execute(
            "SELECT enabled FROM subscriptions WHERE id = ?", (sub_id,)
        )).fetchone()
        sub_enabled = bool(sub_row[0]) if sub_row else True
        if auto_assign and sub_enabled:
            await _assign_query_tx(conn, qid, capacity)
        if sub_enabled:
            await _seed_seen(conn, sub_id, qid)
    return old, qid


async def rebind_subscription(repo, actor, sub_id: str, expected_version: int,
                              qid: str, url: str, price: int, auto_assign: bool,
                              capacity: int, words: str | None = None) -> tuple[str, str]:
    if type(price) is not int or not 0 < price <= 9223372036854775807:
        raise ValueError("Price must be a positive SQLite integer")
    return await repo.writer.submit(lambda conn: rebind_in_transaction(
        conn, actor, sub_id, expected_version, qid, url, price, auto_assign, capacity, words
    ))
