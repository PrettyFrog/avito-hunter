"""Bounded streaming reader and safe teardown for HTTP responses (T-966)."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any


class HttpFetchError(Exception):
    """Base exception for HTTP slot fetch operations."""


class BodySizeLimitExceededError(HttpFetchError):
    """Raised when an HTTP response stream exceeds configured body byte limits."""


async def read_response_content(
    resp: Any, max_body_bytes: int
) -> tuple[bytes, str]:
    """Iterates content chunks enforcing max_body_bytes limit.

    Returns (raw_bytes, decoded_text). Raises any iteration error without masking,
    except benign curl teardown timeouts when complete HTML was already received.
    """
    chunks: list[bytes] = []
    total_bytes = 0
    iterator = resp.aiter_content() if hasattr(resp, "aiter_content") else resp.aiter_bytes()
    try:
        async for chunk in iterator:
            total_bytes += len(chunk)
            if total_bytes > max_body_bytes:
                raise BodySizeLimitExceededError(
                    f"Response body exceeded limit of {max_body_bytes} bytes"
                )
            chunks.append(chunk)
    except BodySizeLimitExceededError:
        raise
    except Exception as exc:
        raw = b"".join(chunks)
        encoding = getattr(resp, "encoding", None) or "utf-8"
        text = raw.decode(encoding, errors="replace")
        err_msg = str(exc).lower()
        if ("curl (28)" in err_msg or "operation timed out" in err_msg) and "</html>" in text.lower():
            return raw, text
        raise
    raw = b"".join(chunks)
    encoding = getattr(resp, "encoding", None) or "utf-8"
    return raw, raw.decode(encoding, errors="replace")


async def safe_teardown_response(resp: Any) -> None:
    """Cancels background stream task and safely closes response without masking primary errors.

    Suppresses child stream cancellation without suppressing caller cancellation.
    """
    if resp is None:
        return
    task = getattr(resp, "astream_task", None)
    if task is not None and hasattr(task, "done") and not task.done():
        if hasattr(task, "cancel"):
            task.cancel()
    if hasattr(resp, "aclose"):
        try:
            await resp.aclose()
        except asyncio.CancelledError:
            cur = asyncio.current_task()
            if cur is not None and getattr(cur, "cancelling", lambda: 0)() > 0:
                raise
        except Exception:
            pass
