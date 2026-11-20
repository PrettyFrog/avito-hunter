import pytest

from hunter.domain.slots import SlotHealth, SlotRole, SlotSnapshot
from hunter.transport.session_store import SessionMetadata, SessionPayload


@pytest.mark.parametrize("view", ("metadata", "slot", "payload"))
def test_slot_and_session_diagnostics_never_expose_proxy_credentials(view):
    endpoint = "http://operator:proxy-password@proxy.example:8080?token=proxy-token"
    metadata = SessionMetadata("slot-a", endpoint, 1, 1, "hash", "2026-10-27T00:00:00Z")
    payload = SessionPayload(metadata=metadata, cookies=[])
    slot = SlotSnapshot("slot-a", endpoint, SlotHealth.READY, SlotRole.ACTIVE)

    views = {
        "metadata": repr(metadata), "slot": repr(slot), "payload": str(payload.redacted_dict())
    }
    output = views[view]
    assert "operator" not in output
    assert "proxy-password" not in output
    assert "proxy-token" not in output

    # The private serialization retains the identity needed to bind cookies to the slot.
    assert metadata.to_dict()["endpoint"] == endpoint
