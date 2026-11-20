"""Test resilience of telegram auth middleware against database is locked errors."""

import sqlite3
import pytest
from unittest.mock import AsyncMock, MagicMock

from hunter.core.db_parts.database_get_user_by_telegram_id_16 import update_user_last_seen_db
from hunter.notify.security_parts.authandscopemiddleware___call_01 import resolve_or_seed_user


@pytest.mark.asyncio
async def test_update_user_last_seen_survives_db_locked():
    """Verify update_user_last_seen_db gracefully handles database is locked."""
    mock_db = MagicMock()
    mock_conn = MagicMock()
    mock_conn.execute = AsyncMock(side_effect=sqlite3.OperationalError("database is locked"))
    mock_db.connect = AsyncMock(return_value=mock_conn)

    # Must not raise sqlite3.OperationalError
    await update_user_last_seen_db(mock_db, user_id=1)


@pytest.mark.asyncio
async def test_resolve_or_seed_user_survives_db_locked():
    """Verify resolve_or_seed_user returns the user even if last_seen update fails."""
    mock_db = MagicMock()
    mock_db.get_user_by_telegram_id = AsyncMock(return_value={
        "id": 1,
        "telegram_user_id": 8735727270,
        "chat_id": 8735727270,
        "role": "owner",
        "status": "active",
        "created_at": "2026-10-30T22:15:59.925255+00:00",
    })
    mock_db.update_user_last_seen = AsyncMock(side_effect=sqlite3.OperationalError("database is locked"))

    user = await resolve_or_seed_user(
        user_id=8735727270,
        from_user=MagicMock(username="owner", full_name="Owner", first_name="Owner"),
        owner_chat_id="8735727270",
        db=mock_db,
    )
    assert user is not None
    assert user.id == 1
    assert user.role == "owner"
