"""Unit tests for baseline semantics and pure price revisions (T-458)."""


from hunter.domain.events import EventType
from hunter.domain.observations import ObservedListing, PriceState
from hunter.domain.transitions import (
    KnownListingState,
    compute_listing_transition,
)


def _obs(
    price: int | None = 10000, title: str = "Title", state: PriceState = PriceState.ACTIVE
) -> ObservedListing:
    return ObservedListing("lot_123", title, price, state, "https://avito.ru/lot_123")


def test_baseline_bootstrap_is_quiet() -> None:
    res = compute_listing_transition(_obs(50000), None, "2026-10-23T10:00:00Z", is_bootstrap=True)
    assert res.event is None and res.next_price == 50000 and res.next_revision == 1


def test_new_listing_and_deferred_mode() -> None:
    obs = _obs(45000)
    res_act = compute_listing_transition(
        obs, None, "2026-10-23T10:00:00Z", is_bootstrap=False, is_paused=False
    )
    assert (
        res_act.event
        and res_act.event.event_type == EventType.NEW_LISTING
        and not res_act.is_deferred
    )

    res_pau = compute_listing_transition(
        obs, None, "2026-10-23T10:00:00Z", is_bootstrap=False, is_paused=True
    )
    assert (
        res_pau.event and res_pau.event.event_type == EventType.NEW_LISTING and res_pau.is_deferred
    )


def test_same_price_generates_zero_events_a_to_a() -> None:
    st = KnownListingState("lot_123", price=50000, price_revision=1)
    res = compute_listing_transition(_obs(50000), st, "2026-10-23T10:05:00Z")
    assert res.event is None and res.next_price == 50000 and res.next_revision == 1


def test_a_to_b_to_a_generates_two_distinct_events() -> None:
    st_a = KnownListingState("lot_123", price=10000, price_revision=1)
    res_b = compute_listing_transition(_obs(9000), st_a, "2026-10-23T10:10:00Z")
    assert res_b.event and res_b.event.event_type == EventType.PRICE_CHANGED
    assert (
        res_b.event.old_price == 10000
        and res_b.event.new_price == 9000
        and res_b.event.revision == 2
    )

    st_b = KnownListingState("lot_123", price=9000, price_revision=res_b.next_revision)
    res_a2 = compute_listing_transition(_obs(10000), st_b, "2026-10-23T10:15:00Z")
    assert res_a2.event and res_a2.event.event_type == EventType.PRICE_CHANGED
    assert (
        res_a2.event.old_price == 9000
        and res_a2.event.new_price == 10000
        and res_a2.event.revision == 3
    )


def test_price_increase_and_decrease() -> None:
    st = KnownListingState("lot_123", price=10000, price_revision=1)
    res_inc = compute_listing_transition(_obs(10001), st, "2026-10-23T10:20:00Z")
    assert res_inc.event and res_inc.event.new_price == 10001 and res_inc.event.revision == 2

    res_dec = compute_listing_transition(_obs(9999), st, "2026-10-23T10:20:00Z")
    assert res_dec.event and res_dec.event.new_price == 9999 and res_dec.event.revision == 2


def test_title_only_change_does_not_trigger_notification() -> None:
    st = KnownListingState("lot_123", price=50000, price_revision=1, title="Initial Title")
    res = compute_listing_transition(_obs(50000, title="New Title"), st, "2026-10-23T10:25:00Z")
    assert res.event is None and res.next_price == 50000


def test_unknown_price_does_not_erase_known_price() -> None:
    st = KnownListingState("lot_123", price=50000, price_revision=1)
    res = compute_listing_transition(
        _obs(None, state=PriceState.UNKNOWN), st, "2026-10-23T10:30:00Z"
    )
    assert res.event is None and res.next_price == 50000 and res.next_revision == 1


def test_reappear_semantics() -> None:
    st = KnownListingState("lot_123", price=50000, price_revision=2)
    assert compute_listing_transition(_obs(50000), st, "2026-10-23T10:35:00Z").event is None
    res_diff = compute_listing_transition(_obs(48000), st, "2026-10-23T10:35:00Z")
    assert res_diff.event and res_diff.event.new_price == 48000 and res_diff.event.revision == 3


def test_previously_unpriced_listing_discovering_price_emits_new_listing() -> None:
    st = KnownListingState("lot_unpriced", price=None, price_state=PriceState.UNKNOWN, price_revision=1)
    res = compute_listing_transition(_obs(1000), st, "2026-11-08T13:58:00Z")
    assert res.event is not None
    assert res.event.event_type == EventType.NEW_LISTING
    assert res.event.old_price is None
    assert res.event.new_price == 1000
    assert res.next_price == 1000
    assert res.next_price_state == PriceState.ACTIVE
    assert res.next_revision == 1


def test_previously_unpriced_listing_discovering_price_during_bootstrap_is_quiet() -> None:
    st = KnownListingState("lot_unpriced", price=None, price_state=PriceState.UNKNOWN, price_revision=1)
    res = compute_listing_transition(_obs(1000), st, "2026-11-08T13:58:00Z", is_bootstrap=True)
    assert res.event is None
    assert res.next_price == 1000
    assert res.next_price_state == PriceState.ACTIVE
    assert res.next_revision == 1


