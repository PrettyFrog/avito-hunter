"""VkNotifier send_notification helper functions (part 1)."""

from __future__ import annotations

import zlib
from typing import Any

import httpx
from loguru import logger


async def _is_owner_user(db: Any, user_id: int) -> bool:
    if not db:
        return False
    try:
        if hasattr(db, "get_user_by_id"):
            u = await db.get_user_by_id(user_id)
            return bool(u and u.get("role") == "owner")
        conn = await db.connect() if hasattr(db, "connect") else db
        cur = await conn.execute("SELECT role FROM users WHERE id = ?", (user_id,))
        row = await cur.fetchone()
        return bool(row and row[0] == "owner")
    except Exception:
        return False


async def resolve_vk_target(
    notifier: Any,
    db: Any,
    recipient_uid: int | None,
) -> int | None:
    from hunter.notify.vk_delivery import get_verified_destination

    if recipient_uid is not None and db:
        try:
            conn = await db.connect() if hasattr(db, "connect") else db
            verified = await get_verified_destination(conn, recipient_uid)
            if verified:
                return verified
        except Exception:
            pass
        if await _is_owner_user(db, recipient_uid):
            return notifier.user_id
        return None
    return notifier.user_id


async def prepare_vk_payload(
    notifier: Any,
    event: Any,
    db: Any,
    recipient_user_id: int | None = None,
) -> tuple[int | None, dict[str, Any] | None]:
    recipient_uid = (
        recipient_user_id
        if recipient_user_id is not None
        else getattr(event, "recipient_user_id", None)
    )
    effective_uid = await resolve_vk_target(notifier, db, recipient_uid)
    if not effective_uid:
        return None, None

    key = (
        f"{event.avito_id}:{recipient_uid}:{effective_uid}:"
        f"{event.verdict}:{event.price}:{event.watchlist_id}"
    )
    random_id = zlib.crc32(key.encode("utf-8")) & 0x7FFFFFFF
    msg_text = notifier.format_message(event)

    data = {
        "access_token": notifier.token,
        "user_id": effective_uid,
        "random_id": random_id,
        "message": msg_text,
        "v": notifier.version,
    }
    return effective_uid, data


async def handle_vk_send_error(
    err: dict[str, Any],
    effective_uid: int,
    recipient_uid: int | None,
    db: Any,
    telegram_notifier: Any,
) -> None:
    from hunter.notify.vk_delivery import handle_vk_error

    if err.get("error_code") == 901 and db and recipient_uid:
        try:
            service = getattr(db, "subscription_service", None)
            repo = getattr(service, "repo", None)
            if repo and hasattr(repo, "set_destination_verified"):
                await repo.set_destination_verified(recipient_uid, "vk", False)
            else:
                c = await db.connect() if hasattr(db, "connect") else db
                await c.execute(
                    "UPDATE channel_destinations SET verified = 0, updated_at = datetime('now') "
                    "WHERE user_id = ? AND channel = 'vk'",
                    (recipient_uid,),
                )
                if hasattr(c, "commit"):
                    await c.commit()
        except Exception:
            pass
    await handle_vk_error(err, effective_uid, db, telegram_notifier)


async def post_vk_message(
    client: httpx.AsyncClient,
    data: dict[str, Any],
    effective_uid: int,
    recipient_uid: int | None,
    event: Any,
    db: Any,
    telegram_notifier: Any,
) -> bool:
    url = "https://api.vk.com/method/messages.send"
    try:
        resp = await client.post(url, data=data)
        res_data = resp.json()
        if "error" in res_data:
            await handle_vk_send_error(
                res_data["error"],
                effective_uid=effective_uid,
                recipient_uid=recipient_uid,
                db=db,
                telegram_notifier=telegram_notifier,
            )
            return False

        logger.info(
            "VK notification delivered for lot {}: message_id={}",
            event.avito_id,
            res_data.get("response"),
        )
        return True
    except Exception as exc:
        logger.warning("VK request failed: {}", exc)
        return False
