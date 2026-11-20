"""Bounded CPU-bound task executor with admission control and cooperative cancellation (T-979)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
import inspect
import threading
from typing import Any, TypeVar

T = TypeVar("T")

_MAX_WORKERS = 2


class CpuBoundExecutor:
    """Bounded thread pool executor with admission control and lifecycle management."""

    def __init__(self, max_workers: int = _MAX_WORKERS) -> None:
        self._max_workers = max_workers
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="hunter-pow-worker")
        self._sem: asyncio.Semaphore | None = None
        self._is_shutdown = False

    @property
    def is_shutdown(self) -> bool:
        return self._is_shutdown

    @property
    def _work_queue(self):
        return self._executor._work_queue

    def _get_semaphore(self) -> asyncio.Semaphore:
        if self._sem is None:
            self._sem = asyncio.Semaphore(self._max_workers)
        return self._sem

    def shutdown(self, wait: bool = False) -> None:
        self._is_shutdown = True
        self._executor.shutdown(wait=wait, cancel_futures=True)
        if self._sem is not None and getattr(self._sem, "_waiters", None):
            for w in list(self._sem._waiters):
                if not w.done():
                    w.set_exception(RuntimeError("cannot schedule new futures after shutdown"))

    def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any):
        if self._is_shutdown:
            raise RuntimeError("cannot schedule new futures after shutdown")
        return self._executor.submit(fn, *args, **kwargs)


_ACTIVE_EXECUTORS: list[CpuBoundExecutor] = []
_LOCK = threading.Lock()


def register_pow_executor(executor: CpuBoundExecutor) -> None:
    with _LOCK:
        if executor not in _ACTIVE_EXECUTORS:
            _ACTIVE_EXECUTORS.append(executor)


def unregister_pow_executor(executor: CpuBoundExecutor | None) -> None:
    with _LOCK:
        if executor is not None:
            if executor in _ACTIVE_EXECUTORS:
                _ACTIVE_EXECUTORS.remove(executor)
        else:
            for ex in list(_ACTIVE_EXECUTORS):
                if ex.is_shutdown:
                    _ACTIVE_EXECUTORS.remove(ex)


def set_pow_executor(executor: CpuBoundExecutor | None) -> None:
    if executor is not None:
        register_pow_executor(executor)
    else:
        unregister_pow_executor(None)


def get_pow_executor() -> CpuBoundExecutor:
    with _LOCK:
        for ex in reversed(_ACTIVE_EXECUTORS):
            if not ex.is_shutdown:
                return ex
        ex = CpuBoundExecutor(max_workers=_MAX_WORKERS)
        _ACTIVE_EXECUTORS.append(ex)
        return ex


def shutdown_pow_executor(wait: bool = False) -> None:
    with _LOCK:
        for ex in list(_ACTIVE_EXECUTORS):
            with suppress(Exception):
                ex.shutdown(wait=wait)
        _ACTIVE_EXECUTORS.clear()


def _call_with_stop_event(
    func: Callable[..., T], args: tuple[Any, ...], kwargs: dict[str, Any], stop_event: threading.Event
) -> T:
    try:
        sig = inspect.signature(func)
        if "stop_event" in sig.parameters:
            kwargs["stop_event"] = stop_event
    except (ValueError, TypeError):
        pass
    return func(*args, **kwargs)


async def run_cpu_bound_cooperative(
    func: Callable[..., T],
    *args: Any,
    timeout_sec: float | None = None,
    executor: CpuBoundExecutor | None = None,
    **kwargs: Any,
) -> T:
    """Executes CPU-intensive function under bounded admission with cooperative cancellation."""
    exec_target = executor or get_pow_executor()
    if exec_target.is_shutdown:
        raise RuntimeError("cannot schedule new futures after shutdown")

    sem = exec_target._get_semaphore()
    loop = asyncio.get_running_loop()
    start_t = loop.time()

    if timeout_sec is not None:
        await asyncio.wait_for(sem.acquire(), timeout=timeout_sec)
    else:
        await sem.acquire()

    released = False

    def release_permit(_future: Any = None) -> None:
        nonlocal released
        if not released:
            released = True
            try:
                if not loop.is_closed():
                    loop.call_soon_threadsafe(sem.release)
            except RuntimeError:
                pass

    if exec_target.is_shutdown:
        release_permit()
        raise RuntimeError("cannot schedule new futures after shutdown")

    stop_event = threading.Event()
    try:
        cf_future = exec_target.submit(_call_with_stop_event, func, args, kwargs, stop_event)
        cf_future.add_done_callback(release_permit)
        future = asyncio.wrap_future(cf_future, loop=loop)
        if timeout_sec is not None:
            rem_timeout = max(0.0001, timeout_sec - (loop.time() - start_t))
            return await asyncio.wait_for(future, timeout=rem_timeout)
        return await future
    except (asyncio.CancelledError, TimeoutError, asyncio.TimeoutError):
        stop_event.set()
        raise
    except Exception:
        release_permit()
        raise
