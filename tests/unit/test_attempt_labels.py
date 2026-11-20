"""Attempt metadata must correlate identities without exposing identifiers."""
from types import SimpleNamespace

import httpx
import pytest


def test_labels_are_opaque_and_unknown_egress_is_not_invented():
    from hunter.transport.attempt_labels import AttemptLabels
    labels = AttemptLabels("private-user-search", "user_b")
    events = []
    labels.emitter(events.append)({"event": "http_attempt_start"})
    assert "private-user-search" not in str(events)
    assert events[0]["identity"] == "user_b"
    assert events[0]["egress_group"] == "unknown"


@pytest.mark.asyncio
async def test_httpx_client_attaches_labels_to_both_events():
    from hunter.transport.attempt_labels import AttemptLabels
    from hunter.transport.http_attempts import instrument_httpx_client
    events = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200))) as client:
        instrument_httpx_client(client, events.append, labels=AttemptLabels("slot", "user_a", "shared-ip"))
        await client.get("https://example.test/")
    assert len(events) == 2
    assert events[0]["slot"] == events[1]["slot"]
    assert events[0]["egress_group"] == events[1]["egress_group"] != "unknown"


@pytest.mark.asyncio
async def test_native_sessions_keep_separate_identity_labels():
    from hunter.transport.attempt_labels import AttemptLabels
    from hunter.transport.observed_curl import ObservedCurlSession
    events = []
    async with ObservedCurlSession(emit=events.append, labels=AttemptLabels("slot", "user_b")) as session:
        curl = await session.pop_curl()
        session.release_curl(curl)
    assert len(events) == 2
    assert {e["identity"] for e in events} == {"user_b"}
