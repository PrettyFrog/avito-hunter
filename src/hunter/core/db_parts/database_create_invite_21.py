"""Database slice 21: Invites and Access Requests operations."""

from collections.abc import Awaitable, Callable
import datetime
from typing import Any
import aiosqlite

from hunter.core.models import now_utc


async def create_invite_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    token_hash: str,
    label: str | None,
    tier: str,
    created_by: int,
    expires_at: str,
    max_uses: int = 1,
) -> int:
    conn = await connect()
    now_str = now_utc().isoformat()
    cur = await conn.execute(
        """
        INSERT INTO invites (token_hash, label, tier, created_by, created_at, expires_at, max_uses, used_count, revoked)
        VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0)
        """,
        (token_hash, label, tier, created_by, now_str, expires_at, max_uses),
    )
    await conn.commit()
    return cur.lastrowid or 0


async def get_invite_by_hash_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    token_hash: str,
) -> dict[str, Any] | None:
    conn = await connect()
    cur = await conn.execute("SELECT * FROM invites WHERE token_hash = ?", (token_hash,))
    row = await cur.fetchone()
    return dict(row) if row else None


async def get_invite_by_id_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    invite_id: int,
) -> dict[str, Any] | None:
    conn = await connect()
    cur = await conn.execute("SELECT * FROM invites WHERE id = ?", (invite_id,))
    row = await cur.fetchone()
    return dict(row) if row else None


async def consume_invite_atomic_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    token_hash: str,
    now_str: str,
) -> bool:
    """Atomically claim and mark an invite as used.
    Returns True if claimed successfully, False if already used, expired, or revoked.
    """
    conn = await connect()
    cur = await conn.execute(
        """
        UPDATE invites
        SET used_count = used_count + 1, used_at = ?
        WHERE token_hash = ?
          AND revoked = 0
          AND used_at IS NULL
          AND used_count < max_uses
          AND expires_at > ?
        """,
        (now_str, token_hash, now_str),
    )
    await conn.commit()
    return (cur.rowcount or 0) > 0


async def get_active_invites_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    created_by: int | None = None,
) -> list[dict[str, Any]]:
    conn = await connect()
    now_str = now_utc().isoformat()
    if created_by:
        cur = await conn.execute(
            """
            SELECT * FROM invites
            WHERE revoked = 0 AND used_at IS NULL AND used_count < max_uses AND expires_at > ? AND created_by = ?
            ORDER BY id DESC
            """,
            (now_str, created_by),
        )
    else:
        cur = await conn.execute(
            """
            SELECT * FROM invites
            WHERE revoked = 0 AND used_at IS NULL AND used_count < max_uses AND expires_at > ?
            ORDER BY id DESC
            """,
            (now_str,),
        )
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_active_invites_count_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    created_by: int | None = None,
) -> int:
    conn = await connect()
    now_str = now_utc().isoformat()
    if created_by:
        cur = await conn.execute(
            """
            SELECT COUNT(*) FROM invites
            WHERE revoked = 0 AND used_at IS NULL AND used_count < max_uses AND expires_at > ? AND created_by = ?
            """,
            (now_str, created_by),
        )
    else:
        cur = await conn.execute(
            """
            SELECT COUNT(*) FROM invites
            WHERE revoked = 0 AND used_at IS NULL AND used_count < max_uses AND expires_at > ?
            """,
            (now_str,),
        )
    row = await cur.fetchone()
    return row[0] if row else 0


async def revoke_invite_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    invite_id: int,
) -> bool:
    conn = await connect()
    cur = await conn.execute(
        "UPDATE invites SET revoked = 1 WHERE id = ? AND revoked = 0",
        (invite_id,),
    )
    await conn.commit()
    return (cur.rowcount or 0) > 0


async def create_access_request_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    telegram_user_id: int,
    username: str | None,
    display_name: str | None,
) -> int:
    conn = await connect()
    now_str = now_utc().isoformat()
    cur = await conn.execute(
        """
        INSERT INTO access_requests (telegram_user_id, username, display_name, created_at, status)
        VALUES (?, ?, ?, ?, 'pending')
        """,
        (telegram_user_id, username, display_name, now_str),
    )
    await conn.commit()
    return cur.lastrowid or 0


async def get_access_request_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    request_id: int,
) -> dict[str, Any] | None:
    conn = await connect()
    cur = await conn.execute("SELECT * FROM access_requests WHERE id = ?", (request_id,))
    row = await cur.fetchone()
    return dict(row) if row else None


async def get_recent_access_request_time_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    telegram_user_id: int,
) -> str | None:
    conn = await connect()
    cur = await conn.execute(
        """
        SELECT created_at FROM access_requests
        WHERE telegram_user_id = ?
        ORDER BY id DESC LIMIT 1
        """,
        (telegram_user_id,),
    )
    row = await cur.fetchone()
    return row[0] if row else None


async def get_rejected_access_requests_count_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
    telegram_user_id: int,
) -> int:
    conn = await connect()
    cur = await conn.execute(
        """
        SELECT COUNT(*) FROM access_requests
        WHERE telegram_user_id = ? AND status = 'rejected'
        """,
        (telegram_user_id,),
    )
    row = await cur.fetchone()
    return row[0] if row else 0


async def get_access_requests_last_hour_count_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
) -> int:
    conn = await connect()
    one_hour_ago = (now_utc() - datetime.timedelta(hours=1)).isoformat()
    cur = await conn.execute(
        "SELECT COUNT(*) FROM access_requests WHERE created_at >= ?",
        (one_hour_ago,),
    )
    row = await cur.fetchone()
    return row[0] if row else 0


async def get_pending_access_requests_count_db(
    connect: Callable[[], Awaitable[aiosqlite.Connection]],
) -> int:
    conn = await connect()
    cur = await conn.execute("SELECT COUNT(*) FROM access_requests WHERE status = 'pending'")
    row = await cur.fetchone()
    return row[0] if row else 0
