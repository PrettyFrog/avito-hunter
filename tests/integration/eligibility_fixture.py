"""Create a real eligible subscription for seeded outbox integration events."""
import json


async def attach_eligible_subscription(conn, user_id, event_id, avito_id, observed_at):
    qid = f"fixture_{event_id}"
    await conn.execute(
        "INSERT INTO queries(id, canonical_url, bootstrap_state) VALUES (?, ?, 'ready')",
        (qid, f"https://avito.ru/{qid}"),
    )
    await conn.execute(
        "INSERT INTO subscriptions(id,user_id,query_id,max_price) VALUES (?, ?, ?, 1000)",
        (f"sub_{event_id}", user_id, qid),
    )
    await conn.execute(
        "INSERT INTO query_membership(query_id,avito_id,first_seen,last_seen) VALUES (?, ?, ?, ?)",
        (qid, avito_id, observed_at, observed_at),
    )
    await conn.execute("UPDATE events SET new_price=100 WHERE id=?", (event_id,))
    await conn.execute(
        "INSERT INTO recipient_events(user_id,event_id) VALUES (?, ?)", (user_id, event_id)
    )
    return json.dumps({"avito_id": avito_id, "new_price": 100, "title": "Fixture",
                       "url": f"https://avito.ru/item/{avito_id}"})
