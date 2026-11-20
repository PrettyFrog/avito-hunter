"""Transactional deletion of public tenant data without touching shared tenants."""

from typing import Any
import aiosqlite

from hunter.core.db_parts.database_get_user_by_telegram_id_16 import (
    _anonymize_user_record,
    _delete_user_related_records,
)


async def _get_existing_tables(conn: aiosqlite.Connection) -> set[str]:
    cur = await conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    return {r[0] for r in await cur.fetchall()}


async def _cleanup_orphaned_groups_and_queries(
    conn: aiosqlite.Connection,
    tables: set[str],
    group_rows: list[Any],
    query_rows: list[Any],
) -> None:
    if "query_groups" in tables:
        for (group_id,) in group_rows:
            await conn.execute(
                "DELETE FROM query_groups WHERE id = ? AND NOT EXISTS "
                "(SELECT 1 FROM group_members WHERE group_id = ?)",
                (group_id, group_id),
            )
    if "queries" in tables:
        for (query_id,) in query_rows:
            await conn.execute(
                "DELETE FROM queries WHERE id = ? AND NOT EXISTS "
                "(SELECT 1 FROM subscriptions WHERE query_id = ?)",
                (query_id, query_id),
            )
            if "assignments" in tables:
                await conn.execute(
                    "UPDATE assignments SET active = 0, updated_at = datetime('now') "
                    "WHERE query_id = ? AND active = 1 AND NOT EXISTS ("
                    "SELECT 1 FROM subscriptions WHERE query_id = ? AND enabled = 1)",
                    (query_id, query_id),
                )


async def delete_public_user(conn: aiosqlite.Connection, user_id: int) -> dict[str, int]:
    user = await (
        await conn.execute(
            "SELECT role, chat_id, telegram_user_id FROM users WHERE id = ?", (user_id,)
        )
    ).fetchone()
    if not user:
        return {}
    if user[0] == "owner":
        raise ValueError("Owner cannot be deleted, blocked, or paused")
    tables = await _get_existing_tables(conn)
    query_rows: list[Any] = []
    if "subscriptions" in tables:
        query_rows = await (
            await conn.execute(
                "SELECT DISTINCT query_id FROM subscriptions WHERE user_id = ?", (user_id,)
            )
        ).fetchall()
    group_rows: list[Any] = []
    if "group_members" in tables:
        group_rows = await (
            await conn.execute(
                "SELECT DISTINCT group_id FROM group_members WHERE user_id = ?", (user_id,)
            )
        ).fetchall()
    stats = await _delete_user_related_records(conn, user_id, user[1], user[2])
    for table in ("deliveries", "recipient_events", "channel_destinations", "subscriptions"):
        if table in tables:
            cur = await conn.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
            stats[table] = cur.rowcount
    if "subscription_tombstones" in tables:
        await conn.execute("DELETE FROM subscription_tombstones WHERE user_id = ?", (user_id,))
    await _cleanup_orphaned_groups_and_queries(conn, tables, group_rows, query_rows)
    if "runtime" in tables:
        await conn.execute("DELETE FROM runtime WHERE key = ?", (f"cutover:tenant:{user_id}",))
    await _anonymize_user_record(conn, user_id)
    return stats
