"""Separate streamed-body termination from the physical request/headers event."""
import asyncio
from collections.abc import Callable

import httpx


class ObservedBody(httpx.AsyncByteStream):
    def __init__(self, inner: httpx.AsyncByteStream, finish: Callable[[str], None]):
        self.inner, self.finish = inner, finish
        self._finished = False

    def _end(self, outcome: str) -> None:
        if not self._finished:
            self._finished = True
            self.finish(outcome)

    async def __aiter__(self):
        outcome = "closed"
        try:
            async for chunk in self.inner:
                yield chunk
            outcome = "complete"
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except Exception:
            outcome = "error"
            raise
        finally:
            self._end(outcome)

    async def aclose(self) -> None:
        outcome = "closed"
        try:
            await self.inner.aclose()
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except Exception:
            outcome = "error"
            raise
        finally:
            self._end(outcome)
