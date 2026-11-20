import pytest
from typer.testing import CliRunner

import hunter.cli as cli
from hunter.storage.slot_state import SlotRepository


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://user:private@proxy:8080",
        "http://proxy:8080/?token=private",
        "http://proxy:8080/private",
        "http://proxy:8080/#private",
    ],
)
def test_cli_rejects_secret_bearing_endpoint_before_admin(endpoint, monkeypatch):
    def unexpected_start(_operation):
        raise AssertionError("invalid endpoint must not start admin")

    monkeypatch.setattr(cli, "_run_slot_admin", unexpected_start)
    result = CliRunner().invoke(cli.app, ["slot", "add", "slot", "--endpoint", endpoint])
    assert result.exit_code == 2
    assert "private" not in result.output


@pytest.mark.asyncio
async def test_repository_rejects_credentials_before_writer():
    class Writer:
        async def submit(self, _transaction):
            raise AssertionError("invalid endpoint must not reach writer")

    repo = SlotRepository(None, Writer())
    with pytest.raises(ValueError):
        await repo.register_slot("slot", "http://user:private@proxy:8080")


def test_cli_accepts_direct_endpoint(monkeypatch):
    recorded = []

    def fake_start(op):
        recorded.append(op)
        from hunter.domain.slots import SlotCapacity, SlotHealth, SlotRole, SlotSnapshot
        return SlotSnapshot(
            slot_id="slot-home",
            endpoint="direct",
            generation=1,
            health=SlotHealth.READY,
            role=SlotRole.ACTIVE,
            session_version=1,
            capacity=SlotCapacity(max_queries=4, assigned_queries=()),
        )

    monkeypatch.setattr(cli, "_run_slot_admin", fake_start)
    result = CliRunner().invoke(cli.app, ["slot", "add", "slot-home", "--endpoint", "direct"])
    assert result.exit_code == 0
    assert len(recorded) == 1
    assert "Added slot slot-home" in result.output
