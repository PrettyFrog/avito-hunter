"""Contract tests for domain types: price, parse, observation, event, subscription."""

from __future__ import annotations

import pytest

from hunter.domain.events import Event, EventType
from hunter.domain.observations import (
    ObservationBatch,
    ObservedListing,
    ParseOutcome,
    PriceState,
)
from hunter.domain.subscriptions import Subscription


def test_parse_outcome_and_price_state_coverage_and_roundtrip():
    """Verify that all ParseOutcome and PriceState values serialize and roundtrip cleanly."""
    for outcome in ParseOutcome:
        for pstate in PriceState:
            listing = ObservedListing(
                avito_id="lot_42",
                title="Test Item",
                price=15000,
                price_state=pstate,
                url="https://avito.ru/42",
            )
            batch = ObservationBatch(
                slot_id="slot_01",
                query_id="q_sony",
                observed_at="2026-10-23T10:00:00Z",
                outcome=outcome,
                listings=(listing,),
                latency_ms=120,
                http_status=200,
            )
            d = batch.to_dict()
            assert d["version"] == 1
            assert d["outcome"] == str(outcome)
            assert d["listings"][0]["price_state"] == str(pstate)

            restored = ObservationBatch.from_dict(d)
            assert restored.outcome == outcome
            assert restored.listings[0].price_state == pstate
            assert restored == batch


def test_observation_batch_validation_and_invalid_payloads():
    """Verify that malformed or unsupported observation batches are rejected."""
    # Negative price rejected
    with pytest.raises(ValueError, match="price cannot be negative"):
        ObservedListing(
            avito_id="lot_1",
            title="Item",
            price=-10,
            price_state=PriceState.ACTIVE,
            url="https://avito.ru/1",
        )

    # Empty avito_id or url rejected
    with pytest.raises(ValueError, match="avito_id must be a non-empty string"):
        ObservedListing(
            avito_id="",
            title="Item",
            price=100,
            price_state=PriceState.ACTIVE,
            url="https://avito.ru/1",
        )

    # Unsupported version rejected
    with pytest.raises(ValueError, match="Unsupported ObservationBatch version"):
        ObservationBatch.from_dict(
            {
                "version": 2,
                "slot_id": "s1",
                "query_id": "q1",
                "observed_at": "2026-10-23T10:00:00Z",
                "outcome": "ok",
            }
        )

    # Invalid outcome rejected
    with pytest.raises(ValueError):
        ObservationBatch.from_dict(
            {
                "version": 1,
                "slot_id": "s1",
                "query_id": "q1",
                "observed_at": "2026-10-23T10:00:00Z",
                "outcome": "magic_unknown_outcome",
            }
        )


def test_event_contracts_only_new_listing_and_price_changed():
    """Contract: exactly two public events exist: NEW_LISTING and PRICE_CHANGED."""
    assert set(EventType) == {EventType.NEW_LISTING, EventType.PRICE_CHANGED}

    # Valid NEW_LISTING
    new_evt = Event(
        event_id="evt_new_1",
        event_type=EventType.NEW_LISTING,
        avito_id="lot_100",
        query_id="q_1",
        created_at="2026-10-23T10:00:00Z",
        new_price=10000,
        old_price=None,
        title="Brand New Phone",
        url="https://avito.ru/100",
    )
    assert new_evt.old_price is None
    d_new = new_evt.to_dict()
    assert d_new["event_type"] == "NEW_LISTING"
    assert Event.from_dict(d_new) == new_evt

    # NEW_LISTING cannot have old_price
    with pytest.raises(ValueError, match="NEW_LISTING event must have old_price=None"):
        Event(
            event_id="evt_bad_new",
            event_type=EventType.NEW_LISTING,
            avito_id="lot_100",
            query_id="q_1",
            created_at="2026-10-23T10:00:00Z",
            new_price=10000,
            old_price=9000,
        )

    # Valid PRICE_CHANGED
    chg_evt = Event(
        event_id="evt_chg_1",
        event_type=EventType.PRICE_CHANGED,
        avito_id="lot_100",
        query_id="q_1",
        created_at="2026-10-23T10:05:00Z",
        new_price=9500,
        old_price=10000,
        revision=2,
    )
    assert chg_evt.new_price == 9500
    assert chg_evt.old_price == 10000
    d_chg = chg_evt.to_dict()
    assert d_chg["event_type"] == "PRICE_CHANGED"
    assert Event.from_dict(d_chg) == chg_evt

    # PRICE_CHANGED requires both prices and old != new
    with pytest.raises(ValueError, match="requires both old_price and new_price"):
        Event(
            event_id="evt_bad_chg",
            event_type=EventType.PRICE_CHANGED,
            avito_id="lot_100",
            query_id="q_1",
            created_at="2026-10-23T10:05:00Z",
            new_price=9500,
            old_price=None,
        )

    with pytest.raises(ValueError, match="requires old_price != new_price"):
        Event(
            event_id="evt_bad_chg2",
            event_type=EventType.PRICE_CHANGED,
            avito_id="lot_100",
            query_id="q_1",
            created_at="2026-10-23T10:05:00Z",
            new_price=10000,
            old_price=10000,
        )


def test_price_transition_a_b_a_sequence_distinct_revisions():
    """A → B → A must create two price events with distinct revisions."""
    evt1 = Event(
        event_id="evt_trans_1",
        event_type=EventType.PRICE_CHANGED,
        avito_id="lot_200",
        query_id="q_1",
        created_at="2026-10-23T10:10:00Z",
        old_price=10000,
        new_price=9000,
        revision=2,
    )
    evt2 = Event(
        event_id="evt_trans_2",
        event_type=EventType.PRICE_CHANGED,
        avito_id="lot_200",
        query_id="q_1",
        created_at="2026-10-23T10:15:00Z",
        old_price=9000,
        new_price=10000,
        revision=3,
    )
    assert evt1.event_id != evt2.event_id
    assert evt1.revision == 2 and evt2.revision == 3
    assert evt1.new_price != evt2.new_price


def test_subscription_contract_and_tenant_isolation():
    """Verify subscription parameters: strict ceiling, manual stop-words, tenant isolation."""
    sub = Subscription(
        id="sub_user1_q1",
        user_id=42,
        query_id="query_ps5",
        search_url="https://avito.ru/all?q=ps5",
        max_price=45000,
        stop_words=("авито доставка", "не включается"),
        enabled=True,
        ai_enrichment_enabled=False,
    )
    assert sub.user_id == 42
    assert sub.max_price == 45000
    assert sub.ai_enrichment_enabled is False

    # Roundtrip
    d = sub.to_dict()
    assert d["max_price"] == 45000
    restored = Subscription.from_dict(d)
    assert restored == sub

    # Negative max_price rejected
    with pytest.raises(ValueError, match="max_price must be a non-negative integer"):
        Subscription(id="s_bad", user_id=1, query_id="q1", search_url="http://url", max_price=-100)

    # Invalid user_id rejected
    with pytest.raises(ValueError, match="user_id must be a positive integer"):
        Subscription(id="s_bad", user_id=0, query_id="q1", search_url="http://url", max_price=500)
