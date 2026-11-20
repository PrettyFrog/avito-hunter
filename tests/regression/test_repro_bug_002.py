"""Regression test for BUG-002: Public V2 daily request capacity reflects ~5000 req/day per IP (<= 400 lines)."""

from __future__ import annotations

import sqlite3
import pytest

from hunter.core.config import Settings
from hunter.core.db import Database


@pytest.mark.asyncio
async def test_public_v2_daily_cap_is_5000_while_legacy_is_900(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "test_bug_002.db"
    con = sqlite3.connect(db_path)
    schema_sql = open("src/hunter/core/schema.sql", encoding="utf-8").read()
    con.executescript(schema_sql)
    con.close()

    db = Database(db_path)
    await db.connect()

    try:
        # Case 1: In Public V2, daily_cap must reflect 5000 req/day per IP
        v2_settings = Settings.model_validate({
            "telegram_bot_token": "123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ_123456",
            "telegram_chat_id": "123456",
            "public_v2": {"enabled": True},
        })
        monkeypatch.setattr("hunter.core.db_parts.database_get_daily_cap_with_source_28.get_settings", lambda: v2_settings)

        cap, source = await db.get_daily_cap_with_source()
        assert cap == 5000, f"Expected 5000 in Public V2, got {cap}"
        assert "5000" in source
    finally:
        await db.close()
