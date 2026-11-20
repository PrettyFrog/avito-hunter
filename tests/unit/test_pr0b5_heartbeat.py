"""Tests for PR-0b-5 Part B: Heartbeat watchdog startup gap reporting."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from ops.heartbeat.heartbeat import main


def test_heartbeat_gap_sent_when_previous_run_exceeds_15_min(tmp_path: Path, monkeypatch) -> None:
    """If last successful heartbeat was > 15 min ago, send outage notice first."""
    monkeypatch.setattr("ops.heartbeat.heartbeat.Path.home", lambda: tmp_path / "fake_home")
    state_file = tmp_path / "hunter_heartbeat.json"
    now_utc = datetime.now(UTC)
    last_success = now_utc - timedelta(minutes=45)

    state = {
        "last_status": "ok",
        "last_success_ts": last_success.isoformat(),
    }
    state_file.write_text(json.dumps(state), encoding="utf-8")

    sent_messages: list[str] = []

    def mock_send(token: str, chat_id: str, message: str, dry_run: bool = False) -> bool:
        sent_messages.append(message)
        return True

    hourly_mock = [("2026-10-09 14:00", 20), ("2026-10-09 15:00", 18)]

    with (
        patch("ops.heartbeat.heartbeat.send_telegram", side_effect=mock_send),
        patch("ops.heartbeat.heartbeat.fetch_remote_hourly_activity", return_value=hourly_mock),
        patch(
            "ops.heartbeat.heartbeat.check_hunter", return_value=(True, "ok", "Все системы в норме")
        ),
        patch("sys.argv", ["heartbeat.py", "--state-file", str(state_file), "--dry-run"]),
    ):
        main()

    # Gap message must be sent first
    assert len(sent_messages) >= 1
    first_msg = sent_messages[0]
    assert "мониторинг был недоступен с" in first_msg
    assert "по" in first_msg
    assert "2026-10-09 14:00: 20" in first_msg or "14:00" in first_msg

    # State file must record new last_success_ts
    updated_state = json.loads(state_file.read_text(encoding="utf-8"))
    assert "last_success_ts" in updated_state
    new_ts = datetime.fromisoformat(updated_state["last_success_ts"])
    assert (datetime.now(UTC) - new_ts).total_seconds() < 60


def test_heartbeat_no_gap_when_within_15_min(tmp_path: Path, monkeypatch) -> None:
    """If last successful heartbeat was <= 15 min ago, do not send outage notice."""
    monkeypatch.setattr("ops.heartbeat.heartbeat.Path.home", lambda: tmp_path / "fake_home")
    state_file = tmp_path / "hunter_heartbeat.json"
    now_utc = datetime.now(UTC)
    last_success = now_utc - timedelta(minutes=5)

    state = {
        "last_status": "ok",
        "last_success_ts": last_success.isoformat(),
    }
    state_file.write_text(json.dumps(state), encoding="utf-8")

    sent_messages: list[str] = []

    def mock_send(token: str, chat_id: str, message: str, dry_run: bool = False) -> bool:
        sent_messages.append(message)
        return True

    with (
        patch("ops.heartbeat.heartbeat.send_telegram", side_effect=mock_send),
        patch(
            "ops.heartbeat.heartbeat.check_hunter", return_value=(True, "ok", "Все системы в норме")
        ),
        patch("sys.argv", ["heartbeat.py", "--state-file", str(state_file), "--dry-run"]),
    ):
        main()

    gap_messages = [m for m in sent_messages if "мониторинг был недоступен" in m]
    assert len(gap_messages) == 0
