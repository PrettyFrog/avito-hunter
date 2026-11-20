import asyncio

import pytest

from hunter.application.dispatcher import DeliveryDispatcher
from hunter.application.slot_worker import SlotWorker


@pytest.mark.asyncio
async def test_slot_stop_propagates_caller_cancellation():
    worker = SlotWorker("slot")
    entered = asyncio.Event()

    async def child():
        entered.set()
        await asyncio.Event().wait()

    worker._task = asyncio.create_task(child())
    await entered.wait()
    stopper = asyncio.create_task(worker.stop())
    await asyncio.sleep(0)
    stopper.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stopper
    assert worker._task is None


@pytest.mark.asyncio
async def test_dispatcher_stop_propagates_caller_cancellation():
    dispatcher = DeliveryDispatcher(repo=None, senders={})
    started = asyncio.Event()
    cleanup = asyncio.Event()

    async def child():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleanup.set()
            await asyncio.Event().wait()

    dispatcher._task = asyncio.create_task(child())
    await started.wait()
    stopper = asyncio.create_task(dispatcher.stop())
    await cleanup.wait()
    stopper.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stopper
