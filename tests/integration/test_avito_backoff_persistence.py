"""Integration tests for slot backoff persistence, escalation, and Retry-After bounds (T-973)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sqlite3
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from hunter.application.public_poll_runtime import PublicPollRuntime, QueryAssignment
from hunter.application.slot_supervisor import SlotSupervisor
from hunter.domain.poll_policy import PollPolicy
from hunter.domain.slots import SlotSignal
from hunter.storage.connection import StorageConfig, StorageConnectionManager
from hunter.storage.slot_state import SlotRepository
from hunter.storage.writer import SQLiteWriterActor


def _mock_resp(status: int = 200, text: str = "", headers: dict[str, str] | None = None, error: str | None = None) -> Any:
    return MagicMock(status_code=status, text=text, headers=headers or {}, error=error, url="https://www.avito.ru/q", elapsed_sec=0.05, cookies={})


@pytest.fixture
async def slot_env(tmp_path: Path):
    db = tmp_path / "t.db"
    with sqlite3.connect(db) as c: c.executescript(Path("src/hunter/core/schema.sql").read_text())
    mgr = StorageConnectionManager(StorageConfig(db_path=db))
    writer = SQLiteWriterActor(mgr)
    await writer.start()
    try: yield SlotRepository(mgr, writer), mgr, writer, db
    finally:
        await writer.stop()
        await mgr.drain_and_close()


@pytest.mark.asyncio
async def test_consecutive_403_escalates_and_persists_across_restart(slot_env) -> None:
    repo, mgr, writer, db_path = slot_env
    await repo.register_slot("slot_esc", "direct")
    sup = SlotSupervisor(repo)

    for i, exp in enumerate([62, 120, 300, 300], start=1):
        await sup.handle_slot_signal("slot_esc", SlotSignal.HTTP_403)
        s = await repo.get_slot("slot_esc")
        assert s and s.consecutive_errors == i
        cooldown = round((datetime.fromisoformat(s.quarantined_until) - datetime.fromisoformat(s.updated_at)).total_seconds())
        assert cooldown == exp

    await writer.stop()
    await mgr.drain_and_close()

    new_mgr = StorageConnectionManager(StorageConfig(db_path=db_path))
    new_writer = SQLiteWriterActor(new_mgr)
    await new_writer.start()
    try:
        restarted_repo = SlotRepository(new_mgr, new_writer)
        restored = await restarted_repo.get_slot("slot_esc")
        assert restored and restored.consecutive_errors == 4
        await SlotSupervisor(restarted_repo).handle_slot_signal("slot_esc", SlotSignal.PROBE_OK)
        reset = await restarted_repo.get_slot("slot_esc")
        assert reset and reset.consecutive_errors == 0 and reset.consecutive_successes >= 1
    finally:
        await new_writer.stop()
        await new_mgr.drain_and_close()


@pytest.mark.asyncio
async def test_runtime_wires_retry_after_header(slot_env) -> None:
    repo, _, _, _ = slot_env
    await repo.register_slot("slot_ra_rt", "direct")
    gen = await repo.assign_query("slot_ra_rt", "q_ra")
    sup = SlotSupervisor(repo)

    pool = MagicMock(get_fetcher=MagicMock(return_value=MagicMock(fetch=AsyncMock(return_value=_mock_resp(429, "Rate limited", {"Retry-After": "900"})))))
    runtime = PublicPollRuntime(conn_mgr=None, http_pool=pool, ingest=AsyncMock(), policy=PollPolicy(1, 1), slot_signal_handler=sup.handle_slot_signal)
    await runtime.poll(QueryAssignment("slot_ra_rt", "direct", "q_ra", "https://www.avito.ru/q_ra", gen, 0), 0)

    s = await repo.get_slot("slot_ra_rt")
    assert s and s.quarantined_until and round((datetime.fromisoformat(s.quarantined_until) - datetime.fromisoformat(s.updated_at)).total_seconds()) >= 900


@pytest.mark.asyncio
async def test_runtime_wires_primary_errors_and_5xx(slot_env) -> None:
    repo, _, _, _ = slot_env
    await repo.register_slot("slot_err", "direct")
    sup = SlotSupervisor(repo)
    signals: list[SlotSignal] = []

    async def tracking(s_id: str, sig: SlotSignal, **kw: Any) -> Any:
        signals.append(sig)
        return await sup.handle_slot_signal(s_id, sig, **kw)

    pool = MagicMock(get_fetcher=MagicMock(return_value=MagicMock(fetch=AsyncMock(side_effect=[_mock_resp(0, error="Reset"), _mock_resp(503)]))))
    runtime = PublicPollRuntime(conn_mgr=None, http_pool=pool, ingest=AsyncMock(), policy=PollPolicy(1, 1), slot_signal_handler=tracking)
    asg = QueryAssignment("slot_err", "direct", "q_err", "https://www.avito.ru/q_err", 1, 0)
    await runtime.poll(asg, 0)
    await runtime.poll(asg, 1)
    assert signals == [SlotSignal.TIMEOUT, SlotSignal.TIMEOUT]


@pytest.mark.asyncio
async def test_runtime_feed_success_resets_backoff_counter(slot_env) -> None:
    repo, _, _, _ = slot_env
    await repo.register_slot("slot_ok_rt", "direct")
    gen = await repo.assign_query("slot_ok_rt", "q_ok")
    sup = SlotSupervisor(repo)
    for _ in range(3):
        await sup.handle_slot_signal("slot_ok_rt", SlotSignal.HTTP_403, query_id="q_ok", assignment_generation=gen)

    feed_html = (Path(__file__).resolve().parents[1] / "fixtures" / "html" / "search_sample.html").read_text(encoding="utf-8")
    pool = MagicMock(get_fetcher=MagicMock(return_value=MagicMock(fetch=AsyncMock(return_value=_mock_resp(200, feed_html)))))
    runtime = PublicPollRuntime(conn_mgr=None, http_pool=pool, ingest=AsyncMock(), policy=PollPolicy(1, 1), slot_signal_handler=sup.handle_slot_signal)
    await runtime.poll(QueryAssignment("slot_ok_rt", "direct", "q_ok", "https://www.avito.ru/q_ok", gen, 0), 0)

    reset_slot = await repo.get_slot("slot_ok_rt")
    assert reset_slot and reset_slot.consecutive_errors == 0 and reset_slot.consecutive_successes >= 1


@pytest.mark.asyncio
async def test_runtime_wires_503_retry_after_lower_bound(slot_env) -> None:
    repo, _, _, _ = slot_env
    await repo.register_slot("slot_503", "direct")
    sup = SlotSupervisor(repo)
    durations: list[float | None] = []

    async def tracking(s_id: str, sig: SlotSignal, **kw: Any) -> Any:
        durations.append(kw.get("quarantine_duration_sec"))
        return await sup.handle_slot_signal(s_id, sig, **kw)

    resp_503 = _mock_resp(503, headers={"Retry-After": "900"})
    pool = MagicMock(get_fetcher=MagicMock(return_value=MagicMock(fetch=AsyncMock(return_value=resp_503))))
    runtime = PublicPollRuntime(conn_mgr=None, http_pool=pool, ingest=AsyncMock(), policy=PollPolicy(1, 1), slot_signal_handler=tracking)
    asg = QueryAssignment("slot_503", "direct", "q_503", "https://www.avito.ru/q_503", 1, 0)
    await runtime.poll(asg, 0)
    assert durations == [900.0]
