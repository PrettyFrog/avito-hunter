"""Revalidate queued public deliveries against current tenant subscriptions."""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import Any

from hunter.domain.filtering import PublicFilter, evaluate_public_filters
from hunter.domain.observations import ObservedListing, PriceState
from hunter.storage.deliveries import ClaimedDelivery

logger = logging.getLogger(__name__)


class DeliveryEligibility(StrEnum):
    ELIGIBLE = "eligible"
    DEFER = "defer"
    SUPPRESS = "suppress"


async def _check_in_snapshot(conn: Any, delivery: ClaimedDelivery) -> DeliveryEligibility:
    tbl_cur = await conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'"
    )
    if await tbl_cur.fetchone():
        u_row = await (
            await conn.execute("SELECT status FROM users WHERE id = ?", (delivery.user_id,))
        ).fetchone()
        if not u_row or u_row[0] != "active":
            return DeliveryEligibility.SUPPRESS

    event = await (
        await conn.execute(
            "SELECT e.avito_id, e.new_price, e.observed_at FROM events e "
            "JOIN recipient_events r ON r.event_id = e.id AND r.user_id = ? WHERE e.id = ?",
            (delivery.user_id, delivery.event_id),
        )
    ).fetchone()
    if not event:
        return DeliveryEligibility.DEFER
    avito_id, price, observed_at = event
    payload = delivery.payload
    if (
        payload.get("avito_id") != avito_id
        or type(price) is not int
        or price <= 0
        or type(payload.get("new_price")) is not int
        or payload.get("new_price") != price
        or not isinstance(payload.get("title"), str)
        or not isinstance(payload.get("url"), str)
        or not payload["url"]
    ):
        return DeliveryEligibility.DEFER
    listing = ObservedListing(
        str(avito_id), payload["title"], price, PriceState.ACTIVE, payload["url"]
    )
    rows = await (
        await conn.execute(
            "SELECT s.max_price, s.manual_stopwords, q.bootstrap_state, "
            "CASE WHEN julianday(m.last_seen) >= julianday(?) THEN 1 ELSE 0 END, "
            "COALESCE(s.manual_keywords, '') "
            "FROM subscriptions s LEFT JOIN queries q ON q.id = s.query_id "
            "LEFT JOIN query_membership m ON m.query_id = s.query_id AND m.avito_id = ? "
            "WHERE s.user_id = ? AND s.enabled = 1",
            (observed_at, avito_id, delivery.user_id),
        )
    ).fetchall()
    possible = False
    for ceiling, stopwords, state, confirmed, keywords_raw in rows:
        words = tuple(word.strip() for word in (stopwords or "").split(",") if word.strip())
        keywords = tuple(kw.strip() for kw in (keywords_raw or "").split(",") if kw.strip())
        if not evaluate_public_filters(listing, PublicFilter(int(ceiling), words, keywords)).passed:
            continue
        possible = True
        if state == "ready" and confirmed:
            return DeliveryEligibility.ELIGIBLE
    return DeliveryEligibility.DEFER if possible else DeliveryEligibility.SUPPRESS


async def check_delivery_eligibility(mgr: Any, delivery: ClaimedDelivery) -> DeliveryEligibility:
    """Return defer on unavailable or malformed evidence; never claim a delivery sent."""
    try:
        conn = await mgr.open_read_connection()
        try:
            await conn.execute("BEGIN")
            return await _check_in_snapshot(conn, delivery)
        finally:
            try:
                await conn.rollback()
            finally:
                await mgr.close_reader(conn)
    except Exception:
        logger.warning("Could not verify queued delivery eligibility")
        return DeliveryEligibility.DEFER


async def suppress_if_still_ineligible(repo: Any, delivery: ClaimedDelivery) -> bool:
    """Fence suppression against concurrent subscription edits and expired leases."""
    async def _tx(conn: Any) -> bool:
        if await _check_in_snapshot(conn, delivery) != DeliveryEligibility.SUPPRESS:
            return False
        cur = await conn.execute(
            "UPDATE deliveries SET status = 'suppressed', lease_token = NULL, "
            "lease_until = NULL WHERE id = ? AND status = 'leased' AND lease_token = ?",
            (delivery.id, delivery.lease_token),
        )
        return cur.rowcount == 1
    return await repo.writer.submit(_tx)
