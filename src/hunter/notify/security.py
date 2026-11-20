from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import ChatMemberUpdated, TelegramObject
from loguru import logger

from hunter.core.db import get_db
from hunter.core.models import User
from hunter.core.product import ProductProfile, ProductStore, get_product_store
from hunter.notify.security_parts.authandscopemiddleware___call_01 import (
    check_registered_user_role,
    check_scope_and_rate,
    check_user_status_allowed,
    handle_inner_stranger,
    handle_outer_stranger_or_dispatch,
    is_outer_middleware,
    resolve_or_seed_user,
    safe_dispatch_handler,
)

__all__ = [
    "AuthAndScopeMiddleware",
    "GLOBAL_STRANGER_LIMITER",
    "HandlerDef",
    "REGISTERED_HANDLERS",
    "StrangerLimiter",
    "get_owned_product",
    "requires",
]


class HandlerDef:
    def __init__(
        self, handler: Callable[..., Any], name: str, role: str, touches_product_id: bool
    ) -> None:
        self.handler = handler
        self.name = name
        self.role = role
        self.touches_product_id = touches_product_id

    def __repr__(self) -> str:
        return (
            f"<HandlerDef {self.name} role={self.role} touches_product={self.touches_product_id}>"
        )


REGISTERED_HANDLERS: list[HandlerDef] = []


def requires(role: str, touches_product_id: bool = False) -> Callable[..., Any]:
    """Decorator requiring a minimal role ('owner', 'user', or 'public') and recording whether

    the handler accesses/modifies a product by product_id.
    """
    if role not in ("owner", "user", "public"):
        raise ValueError(f"Invalid role '{role}', must be 'owner', 'user', or 'public'")

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        fn._required_role = role  # type: ignore[attr-defined]
        fn._touches_product_id = touches_product_id  # type: ignore[attr-defined]
        REGISTERED_HANDLERS.append(
            HandlerDef(
                handler=fn,
                name=fn.__name__,
                role=role,
                touches_product_id=touches_product_id,
            )
        )
        return fn

    return decorator


def get_owned_product(
    user: User,
    product_id: str,
    store: ProductStore | None = None,
) -> ProductProfile | None:
    """Retrieves a product profile only if the calling user owns it or is the owner.

    Owner has access to all products.
    Regular users have access only to products where p.owner_user_id == user.id.
    Products with owner_user_id=None are considered owned by the system owner.
    """
    if store is None:
        store = get_product_store()
    p = store.get(product_id)
    if not p:
        return None
    if user.role == "owner":
        return p
    if p.owner_user_id is not None and p.owner_user_id == user.id:
        return p
    return None


class StrangerLimiter:
    """In-memory rate limiter and stranger reply throttler.

    Guarantees:
    - Max 60 messages per minute per user (drops bursts/spam).
    - Max 20 stranger replies per hour globally (prevents bot flood).
    - Max 1 reply per stranger every 24 hours (24h cooldown).
    - Max 5 access requests per hour on entire bot (# CALIBRATE).
    - In-memory with max_seen size cap to prevent DB/memory bloat.
    """

    def __init__(
        self,
        user_rate_limit: int = 60,
        stranger_global_limit: int = 20,
        stranger_cooldown_sec: float = 86400.0,
        max_seen_strangers: int = 5000,
        access_requests_per_hour: int = 5,
    ) -> None:
        self.user_rate_limit = user_rate_limit
        self.stranger_global_limit = stranger_global_limit
        self.stranger_cooldown_sec = stranger_cooldown_sec
        self.max_seen_strangers = max_seen_strangers
        self.access_requests_per_hour = access_requests_per_hour
        self._user_msg_times: dict[int, list[float]] = {}
        self._stranger_seen: dict[int, float] = {}
        self._global_replies: list[float] = []
        self._access_requests: list[float] = []
        self._owner_alert_sent_at: float = 0.0

    def check_user_rate(self, user_id: int, now: float | None = None) -> bool:
        if now is None:
            now = time.time()
        times = self._user_msg_times.setdefault(user_id, [])
        cutoff = now - 60.0
        times = [t for t in times if t > cutoff]
        if len(times) >= self.user_rate_limit:
            self._user_msg_times[user_id] = times
            return False
        times.append(now)
        self._user_msg_times[user_id] = times
        if len(self._user_msg_times) > 10000:
            self._user_msg_times = {
                uid: ts for uid, ts in self._user_msg_times.items() if ts and ts[-1] > cutoff
            }
        return True

    def can_reply_to_stranger(self, user_id: int, now: float | None = None) -> bool:
        if now is None:
            now = time.time()
        # 1. 24h cooldown per stranger
        last_seen = self._stranger_seen.get(user_id)
        if last_seen is not None and (now - last_seen) < self.stranger_cooldown_sec:
            return False

        # 2. Global stranger reply rate limit (20/hr)
        cutoff_hr = now - 3600.0
        self._global_replies = [t for t in self._global_replies if t > cutoff_hr]
        if len(self._global_replies) >= self.stranger_global_limit:
            return False

        # Record reply
        self._global_replies.append(now)
        if len(self._stranger_seen) >= self.max_seen_strangers:
            sorted_items = sorted(self._stranger_seen.items(), key=lambda x: x[1])
            self._stranger_seen = dict(sorted_items[int(self.max_seen_strangers * 0.2) :])
        self._stranger_seen[user_id] = now
        return True

    def check_access_request_rate(self, now: float | None = None) -> bool:
        """Rate limit access requests across the bot (max 5 per hour)."""
        if now is None:
            now = time.time()
        cutoff = now - 3600.0
        self._access_requests = [t for t in self._access_requests if t > cutoff]
        if len(self._access_requests) >= self.access_requests_per_hour:
            return False
        self._access_requests.append(now)
        return True

    def should_alert_owner_access_flood(self, now: float | None = None) -> bool:
        """Allow sending at most 1 flood summary alert to owner per hour."""
        if now is None:
            now = time.time()
        if (now - self._owner_alert_sent_at) > 3600.0:
            self._owner_alert_sent_at = now
            return True

    def reset(self) -> None:
        """Reset all in-memory rate limiter and stranger tracking states."""
        self._user_msg_times.clear()
        self._stranger_seen.clear()
        self._global_replies.clear()
        self._access_requests.clear()
        self._owner_alert_sent_at = 0.0

    def record_invalid_invite_attempt(self, user_id: int, now: float | None = None) -> None:
        """Count invalid invite attempts into stranger throttling to prevent token guessing."""
        if now is None:
            now = time.time()
        self._stranger_seen[user_id] = now


GLOBAL_STRANGER_LIMITER = StrangerLimiter()


async def _authenticate_and_check_status(
    mw: Any,
    event: TelegramObject,
    data: dict[str, Any],
) -> tuple[int | None, User | None]:
    user_id = await check_scope_and_rate(event, data, mw.limiter)
    if user_id is None:
        return None, None

    from_user = getattr(event, "from_user", None)
    db = data.get("db") or getattr(mw, "db", None) or get_db()
    current_user = await resolve_or_seed_user(
        user_id=user_id,
        from_user=from_user,
        owner_chat_id=mw.owner_chat_id,
        db=db,
    )
    data["current_user"] = current_user
    data["db"] = db

    if not await check_user_status_allowed(current_user, user_id, event):
        return None, None

    if current_user and current_user.status == "active":
        try:
            await db.update_user_last_seen(current_user.id)
        except Exception as exc:
            logger.debug("Failed updating user last_seen in middleware: {}", exc)

    return user_id, current_user


async def _dispatch_inner_handler(
    handler: Callable[[TelegramObject, dict[str, Any]], Any],
    event: TelegramObject,
    data: dict[str, Any],
    callback_fn: Any,
    callback_name: str,
    current_user: User | None,
    user_id: int,
    limiter: Any,
) -> Any:
    required_role = getattr(callback_fn, "_required_role", None)
    if not required_role:
        logger.critical(
            "RUNTIME DEFAULT DENY: Handler '{}' has no declared role! Dropping update.",
            callback_name or str(callback_fn),
        )
        return None

    if current_user is None:
        return await handle_inner_stranger(
            handler=handler,
            event=event,
            data=data,
            user_id=user_id,
            required_role=required_role,
            limiter=limiter,
        )

    if not await check_registered_user_role(
        current_user=current_user,
        required_role=required_role,
        user_id=user_id,
        callback_name=callback_name,
        event=event,
    ):
        return None

    return await safe_dispatch_handler(
        handler=handler,
        event=event,
        data=data,
        callback_name=callback_name,
    )


class AuthAndScopeMiddleware(BaseMiddleware):
    """Enforces:

    1. Allowed update types: message, callback_query, my_chat_member (others dropped).
    2. Private chats only (group chats/channels silently dropped).
    3. User rate limit (20 msgs/min per user).
    4. Blocked/deleted user drop (status 'blocked' or 'inactive' dropped with absolute silence).
    5. Runtime Default Deny: handler MUST have @_required_role in ('owner', 'user', 'public').
    6. Role enforcement:
       - 'public': allows unauthenticated strangers (e.g. /start, req_access) \
and active users/owner.
       - 'user': requires active user or owner.
       - 'owner': requires owner role.
    7. Stranger throttling: polite refusal once per 24 hours with access request button.
    8. Injects current_user: User | None into handler kwargs.
    """

    def __init__(
        self, owner_chat_id: str | int, limiter: StrangerLimiter | None = None, db: Any = None
    ) -> None:
        self.owner_chat_id = str(owner_chat_id)
        self.limiter = limiter or GLOBAL_STRANGER_LIMITER
        self.db = db

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Any],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user_id, current_user = await _authenticate_and_check_status(self, event, data)
        if user_id is None:
            return None

        if isinstance(event, ChatMemberUpdated):
            return await handler(event, data)

        handler_obj = data.get("handler")
        callback_fn = getattr(handler_obj, "callback", None) if handler_obj else handler
        callback_name = getattr(callback_fn, "__name__", "")
        handler_name = getattr(handler, "__name__", "")

        if is_outer_middleware(handler_obj, callback_name, handler_name):
            return await handle_outer_stranger_or_dispatch(
                handler=handler,
                event=event,
                data=data,
                current_user=current_user,
                user_id=user_id,
                limiter=self.limiter,
            )

        return await _dispatch_inner_handler(
            handler=handler,
            event=event,
            data=data,
            callback_fn=callback_fn,
            callback_name=callback_name,
            current_user=current_user,
            user_id=user_id,
            limiter=self.limiter,
        )
