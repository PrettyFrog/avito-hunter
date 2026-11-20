"""Owner-scoped coalescing for identical service operations, never shared A/B state."""
import asyncio
from collections.abc import Awaitable, Callable
from typing import Any


class ServiceSingleFlight:
    def __init__(self, *, capacity: int = 64):
        if capacity < 1:
            raise ValueError("Positive single-flight capacity required")
        self.capacity = capacity
        self._tasks: dict[tuple[str, str, int, str], asyncio.Task[Any]] = {}
        self._closed = False

    async def run(self, slot_id: str, identity: str, generation: int, operation: str,
                  factory: Callable[[], Awaitable[Any]]) -> Any:
        if identity not in {"user_a", "user_b"} or type(generation) is not int or generation < 0:
            raise ValueError("An explicit identity and generation are required")
        if not slot_id or not operation:
            raise ValueError("An explicit slot and operation are required")
        if self._closed:
            raise RuntimeError("Service coalescer is stopped")
        key = (slot_id, identity, generation, operation)
        task = self._tasks.get(key)
        if task is None:
            if len(self._tasks) >= self.capacity:
                raise RuntimeError("Service coalescer capacity exceeded")
            # Defer invocation until the child task runs, preserving cleanup on sync factory failure.
            async def invoke():
                return await factory()
            task = asyncio.create_task(invoke())
            self._tasks[key] = task
            task.add_done_callback(lambda finished: self._retire(key, finished))
        return await asyncio.shield(task)

    def _retire(self, key, task) -> None:
        if self._tasks.get(key) is task:
            del self._tasks[key]
        if not task.cancelled():
            task.exception()  # Retrieve failures even when every waiter was cancelled.

    async def close(self) -> None:
        self._closed = True
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
