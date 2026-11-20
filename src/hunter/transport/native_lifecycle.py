"""Drain explicitly owned cleanup despite repeated cancellation of its waiter."""
import asyncio

from loguru import logger


async def drain_owned(operation):
    task = asyncio.ensure_future(operation)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as cancelled:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.error("Native owned cleanup failed ({})", type(error).__name__)
            raise cancelled from error
        raise cancelled
