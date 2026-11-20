"""Cleanup failures are observable without exposing exception payloads."""
import logging
from unittest.mock import AsyncMock

import pytest

from hunter.storage.connection import StorageConfig, StorageConnectionManager


@pytest.mark.asyncio
async def test_shutdown_reports_each_failed_cleanup_without_secrets(tmp_path, caplog):
    manager = StorageConnectionManager(StorageConfig(db_path=tmp_path / "unused.db"))
    reader = AsyncMock()
    reader.close.side_effect = OSError("private-proxy-password")
    writer = AsyncMock()
    writer.execute.side_effect = OSError("private-proxy-password")
    writer.close.side_effect = OSError("private-proxy-password")
    manager._readers.add(reader)
    manager._writer = writer
    with caplog.at_level(logging.ERROR, logger="hunter.storage.connection"):
        await manager.drain_and_close()
    messages = [record.getMessage() for record in caplog.records
                if record.name == "hunter.storage.connection"]
    assert len(messages) == 3
    assert any("reader" in message for message in messages)
    assert any("checkpoint" in message for message in messages)
    assert any("writer" in message for message in messages)
    assert "private-proxy-password" not in caplog.text
    assert manager._writer is None and not manager._readers
    reader.close.assert_awaited_once()
    writer.close.assert_awaited_once()
