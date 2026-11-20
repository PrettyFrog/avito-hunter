from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar

from hunter.core.config import get_settings

T = TypeVar("T")

_cpu_executor: ThreadPoolExecutor | None = None
_cpu_executor_workers: int = 0


def get_cpu_executor() -> ThreadPoolExecutor:
    """Return a shared executor for parsing, compression, and file I/O work."""
    global _cpu_executor, _cpu_executor_workers
    workers = 2
    try:
        settings = get_settings()
        workers = max(1, int(getattr(settings.collect, "cpu_workers", 2)))
    except Exception:
        workers = 2

    if _cpu_executor is None or _cpu_executor_workers != workers:
        if _cpu_executor is not None:
            try:
                _cpu_executor.shutdown(wait=False)
            except Exception:
                pass
        _cpu_executor = ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="hunter-cpu",
        )
        _cpu_executor_workers = workers
    return _cpu_executor


async def run_cpu_bound(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Offload a CPU-bound or synchronous disk I/O function to the shared ThreadPoolExecutor."""
    loop = asyncio.get_running_loop()
    call = functools.partial(func, *args, **kwargs) if kwargs else functools.partial(func, *args)
    return await loop.run_in_executor(get_cpu_executor(), call)


def shutdown_cpu_executor(wait: bool = False) -> None:
    """Cleanly shut down the shared ThreadPoolExecutor."""
    global _cpu_executor, _cpu_executor_workers
    if _cpu_executor is not None:
        try:
            _cpu_executor.shutdown(wait=wait)
        except Exception:
            pass
        _cpu_executor = None
        _cpu_executor_workers = 0
