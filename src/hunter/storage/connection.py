"""Connection lifecycle management for SQLite storage (S3 / T-462).

Enforces separate read/write connections, explicit WAL/FK/timeout pragmas,
busy classification, and graceful drain without global singletons.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)


class StorageError(Exception):
    """Base error for storage connection errors."""


@dataclass(frozen=True, slots=True)
class StorageConfig:
    """Explicit SQLite connection parameters."""

    db_path: Path
    busy_timeout_ms: int = 15000
    wal_autocheckpoint: int = 1000
    synchronous: str = "NORMAL"
    foreign_keys: bool = True


def classify_sqlite_error(exc: Exception) -> str:
    """Classify SQLite exception for retry or rollback policies."""
    msg = str(exc).lower()
    if (
        isinstance(exc, sqlite3.OperationalError)
        or "operationalerror" in type(exc).__name__.lower()
    ):
        if "locked" in msg or "busy" in msg:
            return "busy"
        if "corrupt" in msg or "malformed" in msg:
            return "corrupt"
        return "operational"
    if isinstance(exc, sqlite3.IntegrityError) or "integrityerror" in type(exc).__name__.lower():
        return "constraint"
    return "other"


class StorageConnectionManager:
    """Manages dedicated write connection and concurrent read connection pool."""

    def __init__(self, config: StorageConfig) -> None:
        self.config = config
        self._writer: aiosqlite.Connection | None = None
        self._readers: set[aiosqlite.Connection] = set()
        self._lock = asyncio.Lock()
        self._closing = False

    async def open_write_connection(self) -> aiosqlite.Connection:
        """Create or return dedicated write connection with strict write PRAGMAs."""
        async with self._lock:
            if self._closing:
                raise StorageError("Storage is shutting down; cannot open writer")
            if self._writer is not None:
                return self._writer

            self.config.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = await aiosqlite.connect(self.config.db_path)
            try:
                conn.row_factory = aiosqlite.Row
                await conn.execute("PRAGMA journal_mode = WAL")
                await conn.execute(f"PRAGMA synchronous = {self.config.synchronous}")
                await conn.execute(
                    f"PRAGMA foreign_keys = {'ON' if self.config.foreign_keys else 'OFF'}"
                )
                await conn.execute(f"PRAGMA busy_timeout = {self.config.busy_timeout_ms}")
                await conn.execute(f"PRAGMA wal_autocheckpoint = {self.config.wal_autocheckpoint}")
                if self._closing:
                    raise StorageError("Storage is shutting down; cannot open writer")
                self._writer = conn
                return conn
            except BaseException:
                try:
                    await conn.close()
                except Exception:
                    logger.warning("Failed to close writer after initialization failure")
                raise

    async def open_read_connection(self) -> aiosqlite.Connection:
        """Open a separate read connection in query-only mode."""
        if self._closing:
            raise StorageError("Storage is shutting down; cannot open new readers")

        conn = await aiosqlite.connect(self.config.db_path)
        try:
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA query_only = ON")
            await conn.execute(
                f"PRAGMA foreign_keys = {'ON' if self.config.foreign_keys else 'OFF'}"
            )
            await conn.execute(f"PRAGMA busy_timeout = {self.config.busy_timeout_ms}")
            async with self._lock:
                if self._closing:
                    raise StorageError("Storage is shutting down; cannot open new readers")
                self._readers.add(conn)
        except BaseException:
            try:
                await conn.close()
            except Exception:
                logger.warning("Failed to close reader after initialization failure")
            raise
        return conn

    async def close_reader(self, conn: aiosqlite.Connection) -> None:
        """Close an active reader and remove from pool."""
        async with self._lock:
            self._readers.discard(conn)
        try:
            await conn.close()
        except Exception as exc:
            logger.warning("Error closing read connection: %s", exc)

    async def invalidate_writer(self, conn: aiosqlite.Connection) -> None:
        """Discard a connection whose transaction state cannot be trusted."""
        async with self._lock:
            if self._writer is conn:
                self._writer = None
            try:
                await conn.close()
            except Exception:
                logger.error("Failed to close invalid writer connection")

    async def drain_and_close(self) -> None:
        """Drain active readers and close writer cleanly."""
        self._closing = True
        async with self._lock:
            readers_to_close = list(self._readers)
            self._readers.clear()

        for r in readers_to_close:
            try:
                await r.close()
            except Exception:
                logger.error("Failed to close SQLite reader during shutdown")

        async with self._lock:
            if self._writer is not None:
                try:
                    await self._writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except Exception:
                    logger.error("Failed SQLite checkpoint during shutdown")
                try:
                    await self._writer.close()
                except Exception:
                    logger.error("Failed to close SQLite writer during shutdown")
                self._writer = None
