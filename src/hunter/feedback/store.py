from hunter.core.db import Database


async def record_feedback(
    db: Database,
    avito_id: str,
    feedback: str,
    product_id: str | None = None,
    owner_user_id: int | None = None,
) -> None:
    conn = await db.connect()
    cur = await conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {r[0] for r in await cur.fetchall()}
    if not product_id and "listings" in tables:
        cur = await conn.execute(
            "SELECT watchlist_id FROM listings WHERE avito_id = ?", (avito_id,)
        )
        row = await cur.fetchone()
        if row and row[0]:
            product_id = row[0]

    if "notifications" in tables:
        if owner_user_id is not None:
            await conn.execute(
                "UPDATE notifications SET feedback = ? WHERE avito_id = ? AND id = ("
                "SELECT max(id) FROM notifications WHERE avito_id = ? AND "
                "(recipient_user_id = ? OR owner_user_id = ?))",
                (feedback, avito_id, avito_id, owner_user_id, owner_user_id),
            )
        else:
            await conn.execute(
                "UPDATE notifications SET feedback = ? WHERE avito_id = ? AND id = ("
                "SELECT max(id) FROM notifications WHERE avito_id = ?)",
                (feedback, avito_id, avito_id),
            )
    if "deliveries" in tables and owner_user_id is not None:
        try:
            await conn.execute(
                "UPDATE deliveries SET payload = json_set(payload, '$.feedback', ?) "
                "WHERE user_id = ? AND event_id IN (SELECT id FROM events WHERE avito_id = ?)",
                (feedback, owner_user_id, avito_id),
            )
        except Exception:
            pass
    if "listing_events" in tables:
        await db.add_event(
            avito_id,
            "feedback",
            {"feedback": feedback, "product_id": product_id, "owner_user_id": owner_user_id},
        )
    if "feedback" in tables:
        await db.record_feedback_entry(avito_id, feedback, product_id, owner_user_id=owner_user_id)
    await conn.commit()
