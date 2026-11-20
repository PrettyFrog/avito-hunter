"""Single-writer actor for SQLite storage (S3 / T-463)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

import aiosqlite

from hunter.storage.connection import StorageConnectionManager

logger = logging.getLogger(__name__)
T = TypeVar("T")


class WriterError(Exception):
    """Base error for writer actor failures."""


class QueueFullError(WriterError):
    """Raised when writer queue exceeds maximum bounded capacity."""


class WriterStoppedError(WriterError):
    """Raised when submitting commands to a stopped writer."""


@dataclass(slots=True)
class _Envelope(Generic[T]):
    command: Callable[[aiosqlite.Connection], Awaitable[T]]
    future: asyncio.Future[T]
    seq: int


class SQLiteWriterActor:
    """Dedicated single-writer actor owning SQLite write transactions."""

    def __init__(self, connection_manager: StorageConnectionManager, max_queue_size: int = 100) -> None:
        self._conn_mgr = connection_manager
        self._max_queue_size = max_queue_size
        self._queue: asyncio.Queue[_Envelope[Any] | None] = asyncio.Queue(maxsize=max_queue_size)
        self._worker_task: asyncio.Task[None] | None = None
        self._seq_counter = 0
        self._running = False
        self._failed = False

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self) -> None:
        if self._running:
            return
        if self._failed:
            raise WriterStoppedError("Failed writer must be replaced")
        self._running = True
        self._worker_task = asyncio.create_task(self._worker_loop())

    async def stop(self) -> None:
        if not self._running:
            if self._worker_task:
                await self._worker_task
                self._worker_task = None
            return
        self._running = False
        await self._queue.put(None)
        if self._worker_task:
            await self._worker_task
            self._worker_task = None

    async def submit[R](self, command: Callable[[aiosqlite.Connection], Awaitable[R]], *, timeout: float | None = None) -> R:
        if not self._running:
            raise WriterStoppedError("Writer actor is not running")

        loop = asyncio.get_running_loop()
        future: asyncio.Future[R] = loop.create_future()
        self._seq_counter += 1
        env = _Envelope(command, future, self._seq_counter)

        try:
            if timeout is not None:
                await asyncio.wait_for(self._queue.put(env), timeout=timeout)
            else:
                self._queue.put_nowait(env)
        except (asyncio.QueueFull, asyncio.TimeoutError) as exc:
            raise QueueFullError(f"Writer queue capacity ({self._max_queue_size}) exceeded") from exc

        if not self._running and not future.done():
            future.set_exception(WriterStoppedError("Writer actor stopped before execution"))
        return await asyncio.shield(future)

    async def barrier(self) -> int:
        if not self._running:
            raise WriterStoppedError("Writer actor is not running")
        self._seq_counter += 1
        seq = self._seq_counter
        future: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        await self._queue.put(_Envelope(lambda conn: _noop(seq), future, seq))
        if not self._running and not future.done():
            future.set_exception(WriterStoppedError("Writer actor stopped before barrier"))
        return await future

    async def _process_envelope(self, conn: aiosqlite.Connection, env: _Envelope[Any]) -> None:
        if env.future.done():
            return
        try:
            await conn.execute("BEGIN IMMEDIATE")
            result = await env.command(conn)
            await conn.commit()
            if not env.future.done():
                env.future.set_result(result)
        except Exception as exc:
            try:
                if conn.in_transaction:
                    await conn.execute("ROLLBACK")
            except Exception as rollback_error:
                if not env.future.done():
                    env.future.set_exception(WriterError("Writer rollback failed; actor stopped"))
                raise WriterError("Writer rollback failed; actor stopped") from rollback_error
            if not env.future.done():
                env.future.set_exception(exc)

    async def _worker_loop(self) -> None:
        try:
            conn = await self._conn_mgr.open_write_connection()
        except Exception:
            logger.error("SQLite writer could not open its connection")
            self._fail_pending()
            return
        while True:
            env = await self._queue.get()
            if env is None:
                self._queue.task_done()
                break
            try:
                await self._process_envelope(conn, env)
            except asyncio.CancelledError:
                self._running = False
                self._failed = True
                if not env.future.done():
                    env.future.set_exception(WriterStoppedError("Writer command cancelled"))
                logger.error("SQLite writer stopped after command cancellation")
                await self._conn_mgr.invalidate_writer(conn)
                self._fail_pending()
                break
            except WriterError:
                self._running = False
                self._failed = True
                logger.error("SQLite writer stopped after rollback failure")
                await self._conn_mgr.invalidate_writer(conn)
                self._fail_pending()
                break
            finally:
                self._queue.task_done()

    def _fail_pending(self) -> None:
        self._running = False
        self._failed = True
        while not self._queue.empty():
            pending = self._queue.get_nowait()
            if pending is not None and not pending.future.done():
                pending.future.set_exception(WriterStoppedError("Writer actor failed"))
            self._queue.task_done()


async def _noop(val: int) -> int:
    return val
