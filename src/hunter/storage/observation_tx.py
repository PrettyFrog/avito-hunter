"""Atomic transaction handler for observation batch ingestion (S3 / T-464 & T-465)."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import aiosqlite

from hunter.domain.events import Event
from hunter.domain.observations import (
    ObservationBatch,
    ObservedListing,
    ParseOutcome,
    PriceState,
)
from hunter.domain.transitions import (
    KnownListingState,
    compute_listing_transition,
)
from hunter.storage.recipient_fanout import apply_recipient_fanout

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class IngestTxResult:
    """Outcome of an atomic observation transaction."""

    query_id: str
    observed_count: int
    events_generated: tuple[Event, ...]
    is_bootstrap: bool
    stale_sequence: bool = False
    deliveries_created: int = 0
    stale_assignment: bool = False


async def _get_query_bootstrap(conn: aiosqlite.Connection, query_id: str) -> bool:
    cur = await conn.execute("SELECT bootstrap_state FROM queries WHERE id = ?", (query_id,))
    row = await cur.fetchone()
    if not row:
        await conn.execute(
            "INSERT INTO queries (id, canonical_url, bootstrap_state) VALUES (?, ?, 'ready')",
            (query_id, f"https://avito.ru/q/{query_id}"),
        )
        return False
    if row[0] == "pending":
        await conn.execute("UPDATE queries SET bootstrap_state = 'ready' WHERE id = ?", (query_id,))
        return True
    return False


async def _get_known_state(conn: aiosqlite.Connection, avito_id: str) -> KnownListingState | None:
    cur = await conn.execute("SELECT known_price, price_state, revision FROM listing_state WHERE avito_id = ?", (avito_id,))
    row = await cur.fetchone()
    return KnownListingState(avito_id, row[0], PriceState(row[1]), int(row[2])) if row else None


async def _stale_listing_ids(conn: aiosqlite.Connection, batch: ObservationBatch, seq: int) -> set[str]:
    if not batch.listings:
        return set()
    ph = ",".join("?" for _ in batch.listings)
    cur = await conn.execute(
        f"SELECT avito_id, last_request_sequence FROM listing_state WHERE avito_id IN ({ph})",
        [listing.avito_id for listing in batch.listings],
    )
    rows = await cur.fetchall()
    return {str(avito_id) for avito_id, last_seq in rows if seq < int(last_seq or 0)}


async def _has_current_assignment(conn: aiosqlite.Connection, batch: ObservationBatch) -> bool:
    if batch.assignment_generation is None:
        return False
    cur = await conn.execute(
        "SELECT slot_id, generation FROM assignments WHERE query_id = ? AND active = 1",
        (batch.query_id,),
    )
    rows = await cur.fetchall()
    return (
        len(rows) == 1
        and str(rows[0][0]) == batch.slot_id
        and int(rows[0][1]) == batch.assignment_generation
    )


async def _is_query_member(conn: aiosqlite.Connection, query_id: str, avito_id: str) -> bool:
    cur = await conn.execute(
        "SELECT 1 FROM query_membership WHERE query_id = ? AND avito_id = ?",
        (query_id, avito_id),
    )
    return await cur.fetchone() is not None


async def _insert_or_get_event(conn: aiosqlite.Connection, ev: Event, query_id: str) -> Event:
    try:
        await conn.execute(
            """
            INSERT INTO events (id, avito_id, revision, kind, observed_at, old_price, new_price, origin)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (ev.event_id, ev.avito_id, ev.revision, str(ev.event_type), ev.created_at, ev.old_price, ev.new_price, query_id),
        )
    except aiosqlite.IntegrityError:
        cur = await conn.execute(
            "SELECT id FROM events WHERE avito_id = ? AND revision = ? AND kind = ?",
            (ev.avito_id, ev.revision, str(ev.event_type)),
        )
        row = await cur.fetchone()
        if row:
            ev = Event(
                event_id=row[0],
                event_type=ev.event_type,
                avito_id=ev.avito_id,
                query_id=query_id,
                created_at=ev.created_at,
                new_price=ev.new_price,
                old_price=ev.old_price,
                revision=ev.revision,
                title=ev.title,
                url=ev.url,
                location=ev.location,
                image_url=ev.image_url,
            )
    return ev


async def _apply_listing(
    conn: aiosqlite.Connection,
    batch: ObservationBatch,
    listing: ObservedListing,
    is_boot: bool,
    seq: int,
) -> Event | None:
    is_member = (batch.slot_id == "price_watch") or await _is_query_member(conn, batch.query_id, listing.avito_id)
    known_st = await _get_known_state(conn, listing.avito_id) if is_member else None
    trans = compute_listing_transition(
        listing,
        known_st,
        batch.observed_at,
        query_id=batch.query_id,
        is_bootstrap=is_boot,
    )

    await conn.execute(
        """
        INSERT INTO listing_state (avito_id, known_price, price_state, revision, last_request_sequence, updated_at)
        VALUES (?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(avito_id) DO UPDATE SET
            known_price = excluded.known_price,
            price_state = excluded.price_state,
            revision = excluded.revision,
            last_request_sequence = excluded.last_request_sequence,
            updated_at = excluded.updated_at
        """,
        (listing.avito_id, trans.next_price, str(trans.next_price_state), trans.next_revision, seq),
    )

    await conn.execute(
        """
        INSERT INTO query_membership (query_id, avito_id, first_seen, last_seen, baseline_epoch)
        VALUES (?, ?, ?, ?, 0)
        ON CONFLICT(query_id, avito_id) DO UPDATE SET last_seen = excluded.last_seen
        """,
        (batch.query_id, listing.avito_id, batch.observed_at, batch.observed_at),
    )

    if trans.event is not None:
        return await _insert_or_get_event(conn, trans.event, batch.query_id)
    return None


async def execute_observation_tx(
    conn: aiosqlite.Connection,
    batch: ObservationBatch,
    request_sequence: int = 1,
    enforce_assignment: bool = False,
) -> IngestTxResult:
    """Execute atomic observation batch ingestion inside SQLite transaction."""
    if enforce_assignment and not await _has_current_assignment(conn, batch):
        return IngestTxResult(
            query_id=batch.query_id,
            observed_count=0,
            events_generated=(),
            is_bootstrap=False,
            stale_assignment=True,
        )
    if enforce_assignment and batch.outcome in (ParseOutcome.OK, ParseOutcome.EMPTY):
        await conn.execute(
            "INSERT INTO runtime (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (f"query:last_successful_poll:{batch.query_id}", batch.observed_at),
        )
    if batch.outcome == ParseOutcome.EMPTY:
        is_bootstrap = await _get_query_bootstrap(conn, batch.query_id)
        return IngestTxResult(batch.query_id, 0, (), is_bootstrap)
    stale_ids = await _stale_listing_ids(conn, batch, request_sequence)
    fresh_listings = tuple(listing for listing in batch.listings if listing.avito_id not in stale_ids)
    if not fresh_listings:
        return IngestTxResult(
            batch.query_id, 0, (), False, stale_sequence=bool(stale_ids), deliveries_created=0
        )

    is_bootstrap = await _get_query_bootstrap(conn, batch.query_id)
    events: list[Event] = []
    total_deliveries = 0

    for listing in fresh_listings:
        ev = await _apply_listing(conn, batch, listing, is_bootstrap, request_sequence)
        if ev:
            events.append(ev)
        dels = await apply_recipient_fanout(
            conn, batch.query_id, listing, ev, is_bootstrap, batch.observed_at,
            request_sequence, batch.tenant_generations,
        )
        total_deliveries += dels

    return IngestTxResult(
        batch.query_id, len(fresh_listings), tuple(events), is_bootstrap,
        bool(stale_ids), total_deliveries,
    )
