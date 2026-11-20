"""Recipient fanout and subscription eligibility inside observation transaction (S3 / T-465)."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

import aiosqlite

from hunter.domain.delivery_policy import calculate_available_at
from hunter.domain.events import Event
from hunter.domain.filtering import PublicFilter, evaluate_public_filters
from hunter.domain.observations import ObservedListing

logger = logging.getLogger(__name__)


async def get_active_subscriptions(
    conn: aiosqlite.Connection, query_id: str
) -> list[tuple[str, int, int, tuple[str, ...], str, tuple[str, ...]]]:
    cur = await conn.execute(
        "SELECT id, user_id, max_price, manual_stopwords, COALESCE(name, ''), "
        "COALESCE(manual_keywords, '') "
        "FROM subscriptions WHERE query_id = ? AND enabled = 1",
        (query_id,),
    )
    return [
        (
            str(r[0]),
            int(r[1]),
            int(r[2]),
            tuple(w.strip() for w in str(r[3] or "").split(",") if w.strip()),
            str(r[4] or ""),
            tuple(w.strip() for w in str(r[5] or "").split(",") if w.strip()),
        )
        for r in await cur.fetchall()
    ]


async def _get_latest_event_info(conn: aiosqlite.Connection, avito_id: str) -> dict | None:
    cur = await conn.execute(
        "SELECT id, revision, kind, new_price, old_price "
        "FROM events WHERE avito_id = ? ORDER BY revision DESC LIMIT 1",
        (avito_id,),
    )
    r = await cur.fetchone()
    return (
        {"id": r[0], "revision": int(r[1]), "kind": r[2], "new_price": r[3], "old_price": r[4]}
        if r
        else None
    )


async def _record_seen(
    conn: aiosqlite.Connection,
    sub_id: str,
    avito_id: str,
    revision: int,
    ts: str,
    is_baseline: bool,
) -> None:
    epoch = 1 if is_baseline else 0
    sql = (
        "INSERT INTO subscription_seen "
        "(subscription_id, avito_id, baseline_epoch, last_revision, created_at) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(subscription_id, avito_id) "
        "DO UPDATE SET baseline_epoch = CASE WHEN excluded.baseline_epoch = 1 "
        "THEN 1 ELSE subscription_seen.baseline_epoch END, "
        "last_revision = excluded.last_revision"
    )
    await conn.execute(sql, (sub_id, avito_id, epoch, revision, ts))


def _delivery_policy(row: tuple | None, ts: str) -> dict:
    if not row:
        return {"eligible": True, "available_at": ts, "quiet_digest": False}
    status, start_h, end_h, tz_name = row
    if str(status or "active") != "active":
        return {"eligible": False, "available_at": ts}
    created = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    available = calculate_available_at(
        created,
        int(start_h) if start_h is not None else None,
        int(end_h) if end_h is not None else None,
        str(tz_name or "UTC"),
    )
    return {"eligible": True, "available_at": available.isoformat(), "quiet_digest": available > created}


async def _fetch_in(
    conn: aiosqlite.Connection,
    prefix: str,
    values: list[int | str],
    suffix: str = "",
    suffix_params: tuple = (),
) -> list[tuple]:
    rows: list[tuple] = []
    for start in range(0, len(values), 400):
        batch = values[start : start + 400]
        marks = ",".join("?" for _ in batch)
        cursor = await conn.execute(f"{prefix} ({marks}) {suffix}", (*batch, *suffix_params))
        rows.extend(await cursor.fetchall())
    return rows


async def _load_tenant_state(
    conn: aiosqlite.Connection, user_ids: list[int], ts: str
) -> dict[int, dict]:
    users = await _fetch_in(
        conn, "SELECT id, status, quiet_start, quiet_end, timezone FROM users WHERE id IN", user_ids
    )
    policies = {int(row[0]): tuple(row[1:]) for row in users}
    return {
        user_id: {
            "policy": _delivery_policy(policies.get(user_id), ts),
        }
        for user_id in user_ids
    }


async def _load_seen_state(
    conn: aiosqlite.Connection,
    sub_ids: list[str],
    avito_id: str,
    user_ids: list[int],
    event_id: str,
    revision: int = 1,
) -> tuple[dict[str, int], set[int]]:
    seen_rows = await _fetch_in(
        conn,
        "SELECT subscription_id, last_revision FROM subscription_seen WHERE subscription_id IN",
        sub_ids,
        "AND avito_id = ?",
        (avito_id,),
    ) if sub_ids else []
    recipients = await _fetch_in(
        conn,
        "SELECT re.user_id FROM recipient_events re "
        "JOIN events e ON e.id = re.event_id "
        "WHERE re.user_id IN",
        user_ids,
        "AND e.avito_id = ? AND e.revision = ?",
        (avito_id, revision),
    ) if user_ids else []
    return {str(row[0]): int(row[1]) for row in seen_rows}, {int(row[0]) for row in recipients}


async def _create_delivery(
    conn: aiosqlite.Connection,
    user_id: int,
    ev: dict,
    listing: ObservedListing,
    ts: str,
    available_at: str,
    producer_generation: int,
    request_sequence: int,
    quiet_digest: bool,
    threshold_price: int | None = None,
    search_name: str = "",
) -> None:
    await conn.execute(
        "INSERT OR IGNORE INTO recipient_events (user_id, event_id, created_at) VALUES (?, ?, ?)",
        (user_id, ev["id"], ts),
    )
    loc = listing.location
    if not loc and listing.url:
        from hunter.normalize.city import city_from_url
        loc = city_from_url(listing.url)
    p_data: dict[str, Any] = {
        "event_id": ev["id"], "avito_id": listing.avito_id, "kind": ev["kind"],
        "new_price": ev["new_price"], "old_price": ev["old_price"],
        "threshold_price": threshold_price, "url": listing.url, "title": listing.title,
        "image_url": listing.image_url, "location": loc,
        "published_at": listing.published_at, "producer_generation": producer_generation,
        "request_sequence": request_sequence,
    }
    if search_name:
        p_data["search_name"] = search_name
    if listing.raw_snippet:
        for k in ("photos", "description", "delivery", "seller_rating", "seller_reviews", "seller_name", "seller_line", "is_reserved"):
            if k in listing.raw_snippet and listing.raw_snippet[k] is not None:
                p_data[k] = listing.raw_snippet[k]
    if quiet_digest:
        p_data["quiet_digest"] = True
    payload = json.dumps(p_data)
    sql = (
        "INSERT OR IGNORE INTO deliveries "
        "(id, event_id, user_id, channel, destination_version, payload_version, "
        "payload, status, available_at, attempt, created_at) "
        "VALUES (?, ?, ?, 'telegram', COALESCE((SELECT version FROM channel_destinations "
        "WHERE user_id = ? AND channel = 'telegram'), 1), 1, ?, 'pending', ?, 0, ?)"
    )
    await conn.execute(
        sql, (f"del_{ev['id']}_{user_id}_telegram", ev["id"], user_id, user_id, payload, available_at, ts)
    )


async def _get_eligible_recipients(
    conn: aiosqlite.Connection,
    subs: list[Any],
    listing: ObservedListing,
    revision: int,
    info: dict,
    ts: str,
    request_sequence: int,
    tenant_generations: dict[int, int] | None,
) -> dict[int, tuple[int, str, bool, int, str]]:
    user_ids = sorted({sub[1] for sub in subs})
    tenant_state = await _load_tenant_state(conn, user_ids, ts)
    seen, received = await _load_seen_state(
        conn, [sub[0] for sub in subs], listing.avito_id, user_ids, info["id"], revision
    )
    seen_updates: list[tuple] = []
    eligible: dict[int, tuple[int, str, bool, int, str]] = {}
    for sub in subs:
        sid, uid, max_price, words = sub[:4]
        sub_name = sub[4] if len(sub) > 4 else ""
        keywords = sub[5] if len(sub) > 5 else ()
        state = tenant_state.get(uid)
        if not state:
            continue
        user_policy = state["policy"]
        if not user_policy["eligible"]:
            continue
        if seen.get(sid, -1) >= revision:
            continue
        if not evaluate_public_filters(
            listing, PublicFilter(max_price=max_price, manual_stop_words=words, manual_keywords=keywords)
        ).passed:
            continue
        seen_updates.append((sid, listing.avito_id, 0, revision, ts))
        if uid in received:
            continue
        eligible[uid] = (
            0,
            user_policy["available_at"],
            user_policy["quiet_digest"],
            max_price,
            sub_name,
        )
    await _record_seen_many(conn, seen_updates)
    return eligible


async def _record_seen_many(conn: aiosqlite.Connection, rows: list[tuple]) -> None:
    if not rows:
        return
    await conn.executemany(
        "INSERT INTO subscription_seen "
        "(subscription_id, avito_id, baseline_epoch, last_revision, created_at) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(subscription_id, avito_id) "
        "DO UPDATE SET last_revision = excluded.last_revision",
        rows,
    )


async def apply_recipient_fanout(
    conn: aiosqlite.Connection,
    qid: str,
    listing: ObservedListing,
    ev: Event | None,
    is_boot: bool,
    ts: str,
    request_sequence: int = 1,
    tenant_generations: dict[int, int] | None = None,
) -> int:
    """Fan out listing observation to eligible subscriptions inside observation tx."""
    subs = await get_active_subscriptions(conn, qid)
    if not subs:
        return 0
    info = (
        {
            "id": ev.event_id,
            "revision": ev.revision,
            "kind": str(ev.event_type),
            "new_price": ev.new_price,
            "old_price": ev.old_price,
        }
        if ev
        else await _get_latest_event_info(conn, listing.avito_id)
    )
    rev = info["revision"] if info else 1
    if is_boot:
        for sub in subs:
            await _record_seen(conn, sub[0], listing.avito_id, rev, ts, is_baseline=True)
        return 0
    if not info:
        return 0
    eligible = await _get_eligible_recipients(
        conn,
        subs,
        listing,
        rev,
        info,
        ts,
        request_sequence,
        tenant_generations,
    )
    for uid, el_info in eligible.items():
        producer_gen, avail_at, q_digest = el_info[0], el_info[1], el_info[2]
        thresh_price = el_info[3] if len(el_info) > 3 else None
        s_name = el_info[4] if len(el_info) > 4 else ""
        await _create_delivery(
            conn, uid, info, listing, ts, avail_at, producer_gen, request_sequence,
            q_digest, threshold_price=thresh_price, search_name=s_name,
        )
    return len(eligible)
