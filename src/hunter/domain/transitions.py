"""Pure state transitions for listing observations and price revisions."""

from __future__ import annotations

from dataclasses import dataclass

from hunter.domain.events import Event, EventType
from hunter.domain.observations import ObservedListing, PriceState


@dataclass(frozen=True, slots=True)
class KnownListingState:
    avito_id: str
    price: int | None
    price_state: PriceState = PriceState.ACTIVE
    price_revision: int = 1
    is_baseline: bool = False
    title: str | None = None


@dataclass(frozen=True, slots=True)
class TransitionResult:
    event: Event | None
    next_price: int | None
    next_price_state: PriceState
    next_revision: int
    is_deferred: bool = False


def _handle_initial(
    obs: ObservedListing, ts: str, qid: str, is_boot: bool, paused: bool
) -> TransitionResult:
    if is_boot or obs.price is None or obs.price_state != PriceState.ACTIVE:
        return TransitionResult(None, obs.price, obs.price_state, 1, False)

    ev = Event(
        event_id=f"evt_{qid}_{obs.avito_id}_r1",
        event_type=EventType.NEW_LISTING,
        avito_id=obs.avito_id,
        query_id=qid,
        created_at=ts,
        new_price=obs.price,
        old_price=None,
        revision=1,
        title=obs.title,
        url=obs.url,
        location=obs.location,
        image_url=obs.image_url,
    )
    return TransitionResult(ev, obs.price, obs.price_state, 1, paused)


def _handle_existing(
    obs: ObservedListing,
    st: KnownListingState,
    ts: str,
    qid: str,
    paused: bool,
    is_boot: bool = False,
) -> TransitionResult:
    if obs.price is None or obs.price_state != PriceState.ACTIVE:
        return TransitionResult(None, st.price, obs.price_state, st.price_revision, False)

    if obs.price == st.price:
        return TransitionResult(None, st.price, obs.price_state, st.price_revision, False)

    if st.price is None:
        if is_boot:
            return TransitionResult(None, obs.price, obs.price_state, 1, False)
        rev = max(st.price_revision, 1)
        ev = Event(
            event_id=f"evt_{qid}_{obs.avito_id}_r{rev}",
            event_type=EventType.NEW_LISTING,
            avito_id=obs.avito_id,
            query_id=qid,
            created_at=ts,
            new_price=obs.price,
            old_price=None,
            revision=rev,
            title=obs.title,
            url=obs.url,
            location=obs.location,
            image_url=obs.image_url,
        )
        return TransitionResult(ev, obs.price, obs.price_state, rev, paused)

    rev = max(st.price_revision + 1, 2)
    ev = Event(
        event_id=f"evt_{qid}_{obs.avito_id}_r{rev}",
        event_type=EventType.PRICE_CHANGED,
        avito_id=obs.avito_id,
        query_id=qid,
        created_at=ts,
        new_price=obs.price,
        old_price=st.price,
        revision=rev,
        title=obs.title,
        url=obs.url,
        location=obs.location,
        image_url=obs.image_url,
    )
    return TransitionResult(ev, obs.price, obs.price_state, rev, paused)


def compute_listing_transition(
    observation: ObservedListing,
    known_state: KnownListingState | None,
    observed_at: str,
    *,
    query_id: str = "query_default",
    is_bootstrap: bool = False,
    is_paused: bool = False,
) -> TransitionResult:
    """Compute pure transition and return event commands without I/O."""
    if known_state is None:
        return _handle_initial(observation, observed_at, query_id, is_bootstrap, is_paused)
    return _handle_existing(
        observation, known_state, observed_at, query_id, is_paused, is_bootstrap
    )
