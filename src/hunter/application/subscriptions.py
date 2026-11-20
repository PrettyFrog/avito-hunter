"""Application service for public subscriptions (S4 / T-473).

Zero-LLM guarantee:
- Strict price ceiling (exact pmax, zero headroom).
- Manual stop-words only (no AI auto-generation).
- Rejects conflicting URL price and insecure hosts.
- Atomically creates subscription and associates baseline query.
"""

from __future__ import annotations

import hashlib
import inspect
import logging
import uuid
from collections.abc import Sequence
from typing import Any

from hunter.domain.query_url import validate_and_canonicalize_query_url
from hunter.storage.subscriptions import (
    SubscriptionActor,
    SubscriptionRecord,
    SubscriptionRepository,
)

logger = logging.getLogger(__name__)


def compute_query_id(canonical_url: str) -> str:
    """Produces stable query identifier from canonical Avito search URL."""
    return "q_" + hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()[:16]


class SubscriptionService:
    """Tenant-scoped subscription management with strict invariants."""

    def __init__(
        self,
        repo: SubscriptionRepository,
        *,
        auto_assign: bool = False,
        max_queries_per_slot: int = 4,
        on_query_assigned: Any = None,
    ) -> None:
        self.repo = repo
        self.auto_assign = auto_assign
        self.max_queries_per_slot = max_queries_per_slot
        self.on_query_assigned = on_query_assigned

    async def bind_telegram_chat(self, user_id: int, chat_id: int) -> None:
        await self.repo.bind_telegram_chat(user_id, chat_id)

    async def add_subscription(
        self,
        user_id: int,
        url: str,
        max_price: int,
        manual_stopwords: str | Sequence[str] = "",
        *,
        is_owner: bool = False,
        sub_id: str | None = None,
        name: str | None = None,
        manual_keywords: str | Sequence[str] = "",
    ) -> SubscriptionRecord:
        canonical_url = validate_and_canonicalize_query_url(url, max_price)
        qid = compute_query_id(canonical_url)

        if isinstance(manual_stopwords, (list, tuple, set)):
            clean_words = ",".join(
                str(w).strip().lower() for w in manual_stopwords if str(w).strip()
            )
        else:
            clean_words = ",".join(
                str(w).strip().lower() for w in str(manual_stopwords).split(",") if str(w).strip()
            )

        if isinstance(manual_keywords, (list, tuple, set)):
            clean_kws = ",".join(
                str(w).strip().lower() for w in manual_keywords if str(w).strip()
            )
        else:
            clean_kws = ",".join(
                str(w).strip().lower() for w in str(manual_keywords).split(",") if str(w).strip()
            )

        sid = sub_id or f"sub_{uuid.uuid4().hex[:12]}"
        actor = SubscriptionActor(user_id=int(user_id), is_owner=is_owner)
        result = await self.repo.create(
            actor=actor,
            sub_id=sid,
            target_user=int(user_id),
            qid=qid,
            pmax=max_price,
            words=clean_words,
            url=canonical_url,
            auto_assign=self.auto_assign,
            max_queries_per_slot=self.max_queries_per_slot,
            name=name,
            keywords=clean_kws,
        )
        if self.auto_assign and self.on_query_assigned:
            refreshed = self.on_query_assigned(qid)
            if inspect.isawaitable(refreshed):
                await refreshed
        return result

    async def list_subscriptions(
        self, user_id: int, *, is_owner: bool = False
    ) -> list[SubscriptionRecord]:
        actor = SubscriptionActor(user_id=int(user_id), is_owner=is_owner)
        return await self.repo.list_for_actor(actor)

    async def delete_subscription(
        self, user_id: int, sub_id: str, *, is_owner: bool = False
    ) -> None:
        actor = SubscriptionActor(user_id=int(user_id), is_owner=is_owner)
        qid: str | None = None
        if self.auto_assign and self.on_query_assigned:
            try:
                rec = self.repo.get(actor, sub_id)
                if inspect.isawaitable(rec):
                    rec = await rec
                qid = getattr(rec, "query_id", None)
            except Exception:
                qid = None
        await self.repo.delete(actor, sub_id)
        if self.auto_assign and self.on_query_assigned and qid:
            refreshed = self.on_query_assigned(qid)
            if inspect.isawaitable(refreshed):
                await refreshed


async def change_subscription_price(
    service: SubscriptionService, actor: SubscriptionActor, sub_id: str,
    expected_version: int, price: int, words: str | None = None,
) -> tuple[SubscriptionRecord, bool]:
    """Persist a new physical query and reconcile both affected slot workers."""
    from hunter.application.subscription_rebind import change_query_price
    from hunter.storage.subscription_rebind import rebind_subscription

    rec = await service.repo.get(actor, sub_id)
    reader = await service.repo.mgr.open_read_connection()
    try:
        row = await (await reader.execute(
            "SELECT canonical_url FROM queries WHERE id = ?", (rec.query_id,)
        )).fetchone()
        if not row:
            raise ValueError("Поисковый запрос не найден")
        url = change_query_price(str(row[0]), price)
    finally:
        await service.repo.mgr.close_reader(reader)
    old, new = await rebind_subscription(
        service.repo, actor, sub_id, expected_version, compute_query_id(url), url, price,
        service.auto_assign, service.max_queries_per_slot, words,
    )
    refresh_ok = True
    if service.auto_assign and service.on_query_assigned:
        for qid in dict.fromkeys((old, new)):
            try:
                result = service.on_query_assigned(qid)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.error("Failed to refresh workers after subscription change")
                refresh_ok = False
    return await service.repo.get(actor, sub_id), refresh_ok
