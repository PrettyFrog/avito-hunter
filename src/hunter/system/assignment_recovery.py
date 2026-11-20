"""Runtime assignment recovery reconciling DB active assignments with poll runtime workers (T-957)."""

from __future__ import annotations

import logging
from typing import Any

from hunter.application.public_poll_runtime import PublicPollRuntime, load_active_assignments

logger = logging.getLogger(__name__)


def _descriptors_match(runtime: PublicPollRuntime, slot_id: str, entries: list[Any], worker: Any) -> bool:
    db_queries = sorted(item.query_id for item in entries)
    w_queries = sorted(getattr(worker, "assigned_queries", []))
    if db_queries != w_queries:
        return False
    for item in entries:
        existing = runtime.assignments.get((slot_id, item.query_id))
        if existing is None:
            return False
        if (
            existing.endpoint != item.endpoint
            or existing.slot_generation != item.slot_generation
            or existing.generation != item.generation
            or existing.canonical_url != item.canonical_url
            or existing.position != item.position
        ):
            return False
    return True


def _sync_worker_assignments(runtime: PublicPollRuntime, slot_id: str, entries: list[Any], worker: Any) -> None:
    old_entries = {k[1]: v for k, v in runtime.assignments.items() if k[0] == slot_id}
    for k in list(runtime.assignments):
        if k[0] == slot_id:
            runtime.assignments.pop(k, None)
    for item in entries:
        existing = old_entries.get(item.query_id)
        target = existing if (existing and existing.generation > item.generation) else item
        runtime.assignments[(slot_id, target.query_id)] = target
    worker.update_assigned_queries([k[1] for k in runtime.assignments if k[0] == slot_id])


async def reconcile_runtime_assignments(runtime: PublicPollRuntime) -> list[str]:
    """Reconciles persisted active assignments with running runtime workers."""
    if runtime._is_stopped or not runtime.conn_mgr:
        return []

    async with runtime._refresh_lock:
        if runtime._is_stopped:
            return []

        active_assignments = await load_active_assignments(runtime.conn_mgr)
        grouped: dict[str, list[Any]] = {}
        for asg in active_assignments:
            grouped.setdefault(asg.slot_id, []).append(asg)

        recovered_slots: list[str] = []
        for slot_id, entries in grouped.items():
            try:
                worker = runtime.workers.get(slot_id)
                worker_alive = worker is not None and getattr(worker, "_running", False) and worker._task is not None and not worker._task.done()
                if not worker_alive:
                    logger.info("Recovering missing/terminated worker for slot %s", slot_id)
                    await runtime._replace_slot(slot_id, entries, start=True)
                    recovered_slots.append(slot_id)
                else:
                    if not _descriptors_match(runtime, slot_id, entries, worker):
                        old_ep = next((v.endpoint for k, v in runtime.assignments.items() if k[0] == slot_id), None)
                        new_ep = next((item.endpoint for item in entries), None)
                        if old_ep and new_ep and old_ep != new_ep and runtime.http_pool and hasattr(runtime.http_pool, "retire_fetcher"):
                            logger.info("Retiring fetcher for slot %s due to endpoint change: %s -> %s", slot_id, old_ep, new_ep)
                            await runtime.http_pool.retire_fetcher(slot_id)
                        logger.info("Updating assignments in-place for healthy worker on slot %s", slot_id)
                        _sync_worker_assignments(runtime, slot_id, entries, worker)
                        recovered_slots.append(slot_id)
            except Exception:
                logger.exception("Error reconciling slot %s", slot_id)

        for slot_id, worker in list(runtime.workers.items()):
            if slot_id not in grouped:
                try:
                    await worker.stop()
                    runtime.workers.pop(slot_id, None)
                    for k in list(runtime.assignments):
                        if k[0] == slot_id: runtime.assignments.pop(k, None)
                    recovered_slots.append(slot_id)
                except Exception:
                    logger.exception("Error deactivating unassigned worker for slot %s", slot_id)

        return recovered_slots
