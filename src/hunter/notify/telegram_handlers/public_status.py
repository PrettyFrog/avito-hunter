"""Public status, subscription listing, and management for v2 (S4 / T-511)."""

from __future__ import annotations

import html
import inspect
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from hunter.core.config import get_settings
from hunter.core.models import User
from hunter.domain.poll_policy import PollPeriod, PollPolicy
from hunter.storage.connection import StorageConfig, StorageConnectionManager
from hunter.storage.subscription_controls import set_subscription_enabled
from hunter.storage.subscriptions import (
    AccessDeniedError,
    NoActiveSlotCapacityError,
    StaleSubscriptionVersionError,
    SubscriptionActor,
    SubscriptionError,
    SubscriptionNotFoundError,
    SubscriptionRepository,
)
from hunter.storage.writer import SQLiteWriterActor


def _get_repo(db: Any) -> SubscriptionRepository:
    if hasattr(db, "subscription_repo") and db.subscription_repo:
        return db.subscription_repo
    if hasattr(db, "subscription_service") and db.subscription_service:
        return db.subscription_service.repo
    mgr = getattr(db, "storage_mgr", None) or StorageConnectionManager(
        StorageConfig(db_path=Path(getattr(db, "path", "data/hunter.db")))
    )
    return SubscriptionRepository(mgr, getattr(db, "writer", None) or SQLiteWriterActor(mgr))


def format_monitoring_cadence(now_dt: datetime | None = None) -> str:
    settings = get_settings()
    v2 = getattr(settings, "public_v2", None)
    day_sec = getattr(v2, "day_interval_sec", 30.0) if v2 else 30.0
    night_sec = getattr(v2, "night_interval_sec", 180.0) if v2 else 180.0
    period = PollPolicy().get_period(now_dt)
    if period == PollPeriod.DAY:
        sec = int(day_sec) if float(day_sec).is_integer() else day_sec
        return f"Интервал проверки: <b>{sec} с</b> (день)"
    min_val = int(night_sec / 60) if float(night_sec / 60).is_integer() else round(night_sec / 60, 1)
    return f"Интервал проверки: <b>{min_val} мин</b> (ночь)"


def _format_last_successful_poll(value: str | None) -> str:
    if not value:
        return "Последняя успешная проверка: ещё не записана"
    try:
        observed_at = datetime.fromisoformat(value)
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=UTC)
        stamp = observed_at.astimezone(ZoneInfo("Europe/Moscow"))
        return f"Последняя успешная проверка: {stamp:%d.%m %H:%M} МСК"
    except (TypeError, ValueError, OverflowError):
        return "Последняя успешная проверка: время недоступно"


async def get_public_status_text(user: User, db: Any, now_dt: datetime | None = None) -> str:
    repo = _get_repo(db)
    subs = await repo.list_for_actor(
        SubscriptionActor(user_id=user.id)
    )
    cadence = format_monitoring_cadence(now_dt)
    if not subs:
        return (
            f"📡 <b>Статус мониторинга</b>\n{cadence}\n\n"
            "У вас пока нет активных подписок. Добавьте через /add."
        )

    lines = ["📡 <b>Статус мониторинга</b>", cadence, f"Подписок: <b>{len(subs)}</b>\n"]
    reader = await repo.mgr.open_read_connection()
    try:
        for s in subs:
            cur = await reader.execute(
                """SELECT q.bootstrap_state,
                          (SELECT value FROM runtime
                           WHERE key = 'query:last_successful_poll:' || q.id),
                          (SELECT sl.health_state FROM assignments a
                           JOIN slots sl ON sl.id = a.slot_id
                           WHERE a.query_id = q.id AND a.active = 1 LIMIT 1)
                   FROM queries q WHERE q.id = ?""",
                (s.query_id,),
            )
            row = await cur.fetchone()
            bstate = (row[0] if row else "pending").lower()
            last_success = row[1] if row else None
            slot_health = str(row[2] or "").lower() if row else ""
            if slot_health in {"disabled", "quarantined", "auth_required", "challenge", "degraded"}:
                bstate = "degraded"
            st_desc = {
                "pending": "⏳ первый снимок (bootstrap)",
                "waiting": "🕒 ожидание окна проверки",
                "degraded": "⚠️ проверка замедлена",
            }.get(bstate, "🟢 активно")
            p_str, en = (
                f"{s.max_price:,}".replace(",", " "),
                ("🟢 в работе" if s.enabled else "⏸ на паузе"),
            )
            lines.append(
                f"• <b>{html.escape(s.name or 'Без названия')}</b> "
                f"<code>{html.escape(s.id)}</code> | Потолок: <b>{p_str} ₽</b>\n"
                f"  Охват: SERP (поиск) | {en}\n  Состояние: {st_desc}\n"
                f"  {_format_last_successful_poll(last_success)}"
            )
    finally:
        await repo.mgr.close_reader(reader)

    if user.quiet_start is not None and user.quiet_end is not None:
        lines.append(f"\n🌙 Тихие часы: с {user.quiet_start}:00 до {user.quiet_end}:00 МСК")
    return "\n".join(lines)


async def list_public_subscriptions(user: User, db: Any) -> str:
    subs = await _get_repo(db).list_for_actor(
        SubscriptionActor(user_id=user.id)
    )
    if not subs:
        return "📋 Список подписок пуст. Добавьте первую подписку через /add."
    lines = [f"📋 <b>Ваши подписки ({len(subs)} шт.):</b>\n"]
    for s in subs:
        st, w = (
            ("🟢" if s.enabled else "⏸"),
            (f", стоп-слова: {html.escape(s.manual_stopwords)}" if s.manual_stopwords else ""),
        )
        kw = (
            f", ключевые слова: {html.escape(s.manual_keywords)}"
            if getattr(s, "manual_keywords", "")
            else ""
        )
        lines.append(
            f"{st} <b>{html.escape(s.name or 'Без названия')}</b> "
            f"<code>{html.escape(s.id)}</code> — до {s.max_price:,} ₽{w}{kw}".replace(",", " ")
        )
    lines.append("\nУправление: /pause <id>, /resume <id>, /remove <id>, /edit <id> <цена>")
    return "\n".join(lines)


async def _toggle_subscription(
    user: User, sub_id: str, enabled: int, db: Any, act_desc: str
) -> tuple[bool, str]:
    repo = _get_repo(db)
    actor = SubscriptionActor(user_id=user.id, is_owner=(user.role == "owner"))
    try:
        rec = await repo.get(actor, sub_id)
        if rec.enabled == bool(enabled):
            return True, f"Подписка <code>{sub_id}</code> уже в этом состоянии."
        svc = getattr(db, "subscription_service", None)
        auto_assign = getattr(svc, "auto_assign", False) if svc is not None else False
        cb = getattr(svc, "on_query_assigned", None) if (svc and auto_assign) else None
        cap = getattr(svc, "max_queries_per_slot", 4) if svc else 4
        await set_subscription_enabled(
            repo, actor, sub_id, rec.version, bool(enabled),
            auto_assign=auto_assign, max_queries_per_slot=cap, on_query_assigned=cb,
        )
        return True, f"{act_desc} <code>{sub_id}</code>."
    except NoActiveSlotCapacityError:
        return False, "❌ Не удалось возобновить: все слоты сейчас заняты другими активными поисками."
    except (AccessDeniedError, SubscriptionNotFoundError):
        return False, "❌ Подписка не найдена или нет доступа."
    except StaleSubscriptionVersionError:
        return False, "❌ Версия подписки изменилась, повторите попытку."


async def pause_subscription(user: User, sub_id: str, db: Any) -> tuple[bool, str]:
    return await _toggle_subscription(user, sub_id, 0, db, "⏸ Подписка поставлена на паузу")


async def resume_subscription(user: User, sub_id: str, db: Any) -> tuple[bool, str]:
    return await _toggle_subscription(user, sub_id, 1, db, "▶️ Подписка возобновлена")


async def delete_subscription(user: User, sub_id: str, db: Any) -> tuple[bool, str]:
    repo = _get_repo(db)
    try:
        actor = SubscriptionActor(user_id=user.id, is_owner=(user.role == "owner"))
        rec = await repo.get(actor, sub_id)
        await repo.delete(actor, sub_id)
        svc = getattr(db, "subscription_service", None)
        if svc and getattr(svc, "auto_assign", False) and getattr(svc, "on_query_assigned", None):
            res = svc.on_query_assigned(rec.query_id)
            if inspect.isawaitable(res):
                await res
        return True, f"🗑 Подписка <code>{sub_id}</code> успешно удалена."
    except (AccessDeniedError, SubscriptionNotFoundError):
        return False, "❌ Подписка не найдена или нет доступа."


async def edit_subscription(
    user: User, sub_id: str, new_price: int | None, new_words: str | None, db: Any
) -> tuple[bool, str]:
    repo, actor = _get_repo(db), SubscriptionActor(user_id=user.id, is_owner=(user.role == "owner"))
    try:
        rec = await repo.get(actor, sub_id)
        refresh_ok = True
        if new_price is not None:
            from hunter.application.subscriptions import (
                SubscriptionService,
                change_subscription_price,
            )
            service = getattr(db, "subscription_service", None) or SubscriptionService(repo)
            up, refresh_ok = await change_subscription_price(
                service, actor, sub_id, rec.version, new_price, new_words
            )
        else:
            up = await repo.update(actor, sub_id, expected_version=rec.version, words=new_words)
        return (
            True,
            (
                f"✏️ Подписка <code>{sub_id}</code> обновлена: потолок {up.max_price:,} ₽, "
                f"стоп-слова: {up.manual_stopwords or 'нет'}."
                + (" ⚠️ Перезапустите мониторинг: обновить воркеры не удалось."
                   if not refresh_ok else "")
            ).replace(",", " "),
        )
    except (AccessDeniedError, SubscriptionNotFoundError):
        return False, "❌ Подписка не найдена или нет доступа."
    except (SubscriptionError, ValueError, sqlite3.IntegrityError):
        return False, "❌ Не удалось изменить поиск. Проверьте параметры и доступность слотов."
