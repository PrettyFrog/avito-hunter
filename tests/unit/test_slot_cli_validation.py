from typer.testing import CliRunner

import hunter.cli as cli


def test_invalid_slot_id_rejected_before_admin_start(monkeypatch):
    def unexpected_start(_operation):
        raise AssertionError("admin bootstrap must not run for an invalid slot ID")

    monkeypatch.setattr(cli, "_run_slot_admin", unexpected_start)
    result = CliRunner().invoke(
        cli.app, ["slot", "add", "bad/id", "--endpoint", "http://proxy:8080"]
    )
    assert result.exit_code == 2
    assert "Slot ID must contain only" in result.output
