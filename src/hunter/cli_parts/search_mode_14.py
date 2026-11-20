from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

import typer

from hunter.cli_parts.app_01 import app
from hunter.core.config import get_settings
from hunter.core.db import get_db
from hunter.core.models import now_utc


async def _show_search_mode_status(db: Any, cfg_mode: str, streak_limit: int) -> None:
    runtime_mode = await db.get_runtime("search_mode")
    render_until_str = await db.get_runtime("search_mode_render_until")
    streak_str = await db.get_runtime("search_fallback_streak", "0")
    streak_cnt = int(streak_str or "0")

    now = now_utc()
    is_locked_render = False
    remaining_min = 0.0
    if runtime_mode == "render" and render_until_str:
        try:
            ru_dt = datetime.fromisoformat(render_until_str)
            if ru_dt.tzinfo is None:
                ru_dt = ru_dt.replace(tzinfo=UTC)
            if now < ru_dt:
                is_locked_render = True
                remaining_min = max(0.0, (ru_dt - now).total_seconds() / 60.0)
        except Exception:
            pass

    effective = runtime_mode if runtime_mode in ("light", "render") else cfg_mode
    if runtime_mode == "render" and render_until_str and not is_locked_render:
        effective = "light"

    typer.echo("=" * 64)
    typer.echo("🔍 РЕЖИМ ОПРОСА ВЫДАЧ (SERP RETRIEVAL MODE)")
    typer.echo("=" * 64)
    typer.echo(f"Текущий рабочий режим:          {effective.upper()}")
    typer.echo(f"Конфигурация по умолчанию:      {cfg_mode}")
    typer.echo(f"Ручной override в runtime:      {runtime_mode or 'нет (используется config)'}")
    if is_locked_render:
        typer.echo(f"Временный lock на 'render':     ДА (до {render_until_str}, осталось ~{remaining_min:.1f} мин)")
    typer.echo(f"Текущая серия сбоев (streak):   {streak_cnt} / {streak_limit}")
    typer.echo("=" * 64)
    typer.echo("Команды управления:")
    typer.echo("  hunter search-mode light    - принудительно включить лёгкий опрос (сброс серии)")
    typer.echo("  hunter search-mode render   - принудительно включить полный рендер")
    typer.echo("  hunter search-mode status   - показать текущее состояние")
    typer.echo("=" * 64)


async def _search_mode(action: str) -> None:
    action = action.lower().strip()
    db = get_db()
    await db.connect()
    try:
        settings = get_settings()
        cfg_mode = getattr(settings.collect, "search_mode", "light")
        streak_limit = getattr(settings.collect, "search_fallback_streak", 3)

        if action in ("status", "show"):
            await _show_search_mode_status(db, cfg_mode, streak_limit)
        elif action == "light":
            await db.set_runtime("search_mode", "light")
            await db.set_runtime("search_mode_render_until", "")
            await db.set_runtime("search_fallback_streak", "0")
            typer.echo("✅ Режим опроса переключен на 'light'. Серия сбоев сброшена (streak=0).")
        elif action == "render":
            await db.set_runtime("search_mode", "render")
            await db.set_runtime("search_mode_render_until", "")
            typer.echo("✅ Режим опроса переключен на 'render' (полный рендер страниц поиска).")
        else:
            typer.secho(f"❌ Неизвестная команда '{action}'. Допустимо: status, light, render.", fg=typer.colors.RED)
            raise typer.Exit(code=1)
    finally:
        await db.close()


def parse_since_param(since: str | None) -> str | None:
    if not since:
        return None
    if since.endswith("h") and since[:-1].isdigit():
        return (datetime.now(UTC) - timedelta(hours=int(since[:-1]))).isoformat()
    if since.endswith("d") and since[:-1].isdigit():
        return (datetime.now(UTC) - timedelta(days=int(since[:-1]))).isoformat()
    return since


def _fetch_shadow_from_events(cur: sqlite3.Cursor, since_dt: str | None) -> list[dict[str, Any]]:
    event_query = "SELECT avito_id, ts, payload FROM listing_events WHERE kind = 'shadow_notify'"
    params: list[Any] = []
    if since_dt:
        event_query += " AND ts >= ?"
        params.append(since_dt)
    event_query += " ORDER BY ts DESC"
    cur.execute(event_query, params)
    event_rows = cur.fetchall()

    results: list[dict[str, Any]] = []
    for r in event_rows:
        try:
            p = json.loads(r["payload"] or "{}")
        except Exception:
            p = {}
        results.append({
            "avito_id": r["avito_id"],
            "title": p.get("title") or "",
            "price": p.get("price"),
            "watch": p.get("watch_id") or "",
            "model": p.get("model_id") or "",
            "verdict": p.get("verdict") or "",
            "reasons": ", ".join(p.get("reasons") or []),
        })
    return results


def _fetch_shadow_from_evaluations(cur: sqlite3.Cursor, since_dt: str | None) -> list[dict[str, Any]]:
    eval_query = """
        SELECT e.avito_id, l.title, l.price, l.watchlist_id as watch,
               x.model_id as model, e.verdict, e.reasons, e.skip_reason
        FROM evaluations e
        JOIN listings l ON e.avito_id = l.avito_id
        LEFT JOIN extractions x ON e.avito_id = x.avito_id
        WHERE e.verdict IN ('buy_now', 'watch', 'ask_questions')
          AND (e.skip_reason IS NULL OR e.skip_reason = '')
    """
    eval_params: list[Any] = []
    if since_dt:
        eval_query += " AND e.created_at >= ?"
        eval_params.append(since_dt)
    eval_query += " ORDER BY e.id DESC"
    cur.execute(eval_query, eval_params)
    results: list[dict[str, Any]] = []
    for r in cur.fetchall():
        reasons_raw = r["reasons"] or "[]"
        try:
            reasons_list = json.loads(reasons_raw) if isinstance(reasons_raw, str) else []
        except Exception:
            reasons_list = [str(reasons_raw)]
        results.append({
            "avito_id": r["avito_id"],
            "title": r["title"] or "",
            "price": r["price"],
            "watch": r["watch"] or "",
            "model": r["model"] or "",
            "verdict": r["verdict"] or "",
            "reasons": ", ".join(reasons_list[:3]),
        })
    return results


def fetch_shadow_report_candidates(db: Any, since: str | None) -> list[dict[str, Any]] | None:
    if not db.path.exists():
        return None
    since_dt = parse_since_param(since)
    con = sqlite3.connect(f"file:{db.path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        cur = con.cursor()
        results = _fetch_shadow_from_events(cur, since_dt)
        if not results:
            results = _fetch_shadow_from_evaluations(cur, since_dt)
        return results
    finally:
        con.close()


@app.command(name="shadow-report")
def shadow_report_cmd(
    since: str | None = typer.Option(None,
        '--'
        'since', help="Filter since timestamp or duration e.g. 24h"),
) -> None:
    """List lots that would have triggered alerts in shadow mode."""
    db = get_db()
    results = fetch_shadow_report_candidates(db, since)
    if results is None:
        typer.echo("Database not found.")
        return

    if not results:
        typer.echo("No shadow alert candidates found.")
        return

    header = f"{
        'Titl'
        'e':<45} | {'Price':<10} | {'Watch':<25} | {'Model':<20} | {'Verdict':<12} | {'Reasons'}"
    sep = "-" * 135
    typer.echo(sep)
    typer.echo(header)
    typer.echo(sep)
    for row in results:
        t = (row["title"] or "")[:43]
        p = f"{row['price']} ₽" if row["price"] else "N/A"
        w = (row["watch"] or "")[:23]
        m = (row["model"] or "")[:18]
        v = row["verdict"] or ""
        reas = (row["reasons"] or "")[:35]
        typer.echo(f"{t:<45} | {p:<10} | {w:<25} | {m:<20} | {v:<12} | {reas}")
    typer.echo(sep)
    typer.echo(f"Total would-send lots: {len(results)}")


async def _set_muted_status(value: str) -> None:
    db = get_db()
    try:
        await db.set_runtime("muted", value)
    finally:
        await db.close()


@app.command("mute")
def mute_cmd() -> None:
    """Mute live alerts (shadow mode continues)."""
    asyncio.run(_set_muted_status("1"))
    typer.echo("Live alerts muted (shadow mode continues).")


@app.command("unmute")
def unmute_cmd() -> None:
    """Unmute live alerts."""
    asyncio.run(_set_muted_status("0"))
    typer.echo("Live alerts unmuted.")
