"""Keep shared admission owned until a streamed HTTP response finishes."""
from collections.abc import Callable

import httpx


class PermitStream(httpx.AsyncByteStream):
    def __init__(self, inner: httpx.AsyncByteStream, release: Callable[[], object]):
        self.inner, self.release = inner, release

    async def __aiter__(self):
        try:
            async for chunk in self.inner:
                yield chunk
        finally:
            self.release()

    async def aclose(self) -> None:
        try:
            await self.inner.aclose()
        finally:
            self.release()
