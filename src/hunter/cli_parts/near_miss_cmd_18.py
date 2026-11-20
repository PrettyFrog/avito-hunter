from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any
import typer

from hunter.cli_parts.app_01 import app
from hunter.core.config import load_watchlists
from hunter.core.db import Database
from hunter.core.models import now_utc


def parse_near_miss_since(since_str: str | None) -> datetime | None:
    if not since_str:
        return None
    val = since_str.strip().lower()
    if val.endswith("h"):
        return now_utc() - timedelta(hours=float(val[:-1]))
    if val.endswith("d"):
        return now_utc() - timedelta(days=float(val[:-1]))
    if val.endswith("m"):
        return now_utc() - timedelta(minutes=float(val[:-1]))
    if val.isdigit():
        return now_utc() - timedelta(hours=float(val))
    return None


async def _fetch_watch_near_miss_counts(conn: Any, w_id: str, since_iso: str | None) -> tuple[int, int, int, int]:
    # Вошедших
    if since_iso:
        cur = await conn.execute(
            "SELECT count(DISTINCT l.avito_id) FROM listing_events le JOIN listings l ON le.avito_id = l.avito_id WHERE le.kind = 'near_miss' AND l.watchlist_id = ? AND le.ts >= ?",
            (w_id, since_iso),
        )
    else:
        cur = await conn.execute(
            "SELECT count(DISTINCT l.avito_id) FROM listing_events le JOIN listings l ON le.avito_id = l.avito_id WHERE le.kind = 'near_miss' AND l.watchlist_id = ?",
            (w_id,),
        )
    entered_count = (await cur.fetchone())[0]

    # Ещё активных
    cur = await conn.execute(
        "SELECT count(*) FROM listings WHERE watchlist_id = ? AND status = 'near_miss'",
        (w_id,),
    )
    active_count = (await cur.fetchone())[0]

    # Упавших до потолка (с алертом)
    if since_iso:
        cur = await conn.execute(
            """
            SELECT count(DISTINCT l.avito_id)
            FROM listing_events le_nm
            JOIN listings l ON le_nm.avito_id = l.avito_id
            JOIN listing_events le_notif ON l.avito_id = le_notif.avito_id
            WHERE le_nm.kind = 'near_miss' AND le_notif.kind = 'notified'
              AND l.watchlist_id = ? AND le_nm.ts >= ?
            """,
            (w_id, since_iso),
        )
    else:
        cur = await conn.execute(
            """
            SELECT count(DISTINCT l.avito_id)
            FROM listing_events le_nm
            JOIN listings l ON le_nm.avito_id = l.avito_id
            JOIN listing_events le_notif ON l.avito_id = le_notif.avito_id
            WHERE le_nm.kind = 'near_miss' AND le_notif.kind = 'notified'
              AND l.watchlist_id = ?
            """,
            (w_id,),
        )
    dropped_alerted = (await cur.fetchone())[0]

    # Пропавших
    cur = await conn.execute(
        """
        SELECT count(DISTINCT l.avito_id)
        FROM listing_events le
        JOIN listings l ON le.avito_id = l.avito_id
        WHERE le.kind = 'near_miss' AND l.watchlist_id = ? AND l.status = 'gone'
        """,
        (w_id,),
    )
    gone_count = (await cur.fetchone())[0]
    return entered_count, active_count, dropped_alerted, gone_count


async def render_near_miss_summary_by_watch(
    conn: Any,
    watches: dict[str, Any],
    since_iso: str | None,
) -> None:
    summary_header = f"{'Watchlist ID':<35} | {
        'Вошло в '
        'near_miss':<18} | {'Ещё активных':<14} | {'Упало (алерт)':<15} | {'Пропавших':<10}"
    sep_sum = "-" * len(summary_header)
    typer.echo(sep_sum)
    typer.echo(summary_header)
    typer.echo(sep_sum)

    for w_id, watch in watches.items():
        if getattr(watch, "price_headroom_pct", 0) <= 0:
            continue
        entered, active, dropped, gone = await _fetch_watch_near_miss_counts(conn, w_id, since_iso)
        typer.echo(
            f"{w_id:<35} | {entered:<18} | {active:<14} | {dropped:<15} | {gone:<10}"
        )
    typer.echo(sep_sum)


async def render_near_miss_top_10(conn: Any, watches: dict[str, Any]) -> None:
    typer.echo("\nТоп-10 ближайших к потолку лотов:")
    cur = await conn.execute(
        """
        SELECT watchlist_id, title, price
        FROM listings
        WHERE status = 'near_miss'
        """
    )
    raw_active = await cur.fetchall()
    active_lots = []
    for r in raw_active:
        w_id, title, price = r[0], r[1] or "", r[2] or 0
        w = watches.get(w_id)
        max_p = w.max_price if w else 0
        if max_p > 0 and price > max_p:
            pct = (price - max_p) / max_p * 100
            active_lots.append((w_id, title, price, max_p, pct))

    active_lots.sort(key=lambda x: x[4])
    top_10 = active_lots[:10]

    if not top_10:
        typer.echo("Нет активных лотов в near_miss")
        return

    top_header = f"{
        'Watc'
        'h':<25} | {'Title':<40} | {'Цена':<10} | {'Потолок':<10} | {'% до потолка':<12}"
    sep_top = "-" * len(top_header)
    typer.echo(sep_top)
    typer.echo(top_header)
    typer.echo(sep_top)
    for w_id, title, price, max_p, pct in top_10:
        title_tr = (title[:37] + "...") if len(title) > 40 else title
        typer.echo(
            f"{w_id:<25} | {title_tr:<40} | {f'{price} ₽':<10} | {f'{max_p} ₽':<10} | {f'+{pct:.1f}%':<12}"
        )
    typer.echo(sep_top)


async def render_near_miss_default_table(conn: Any, watches: dict[str, Any]) -> None:
    cur = await conn.execute(
        """
        SELECT watchlist_id, title, price, first_seen, last_seen
        FROM listings
        WHERE status = 'near_miss'
        ORDER BY last_seen DESC
        """
    )
    rows = await cur.fetchall()

    if not rows:
        typer.echo("Нет лотов в статусе near_miss")
        return

    header = f"{'Watch':<20} | {'Title':<35} | {'Текущая цена':<13} | {
        'Потоло'
        'к':<10} | {'% до потолка':<13} | {'First Seen':<19} | {'Last Seen':<19}"
    sep = "-" * len(header)
    typer.echo(sep)
    typer.echo(header)
    typer.echo(sep)

    for r in rows:
        watch_id, title, price, first_seen, last_seen = r[0], r[1] or "", r[2], r[3], r[4]
        watch = watches.get(watch_id)
        max_p = watch.max_price if watch else 0
        pct_str = f"+{((price - max_p) / max_p * 100):.1f}%" if max_p else "N/A"
        title_trunc = (title[:32] + "...") if len(title) > 35 else title
        first_trunc = (first_seen[:19].replace("T", " ")) if first_seen else ""
        last_trunc = (last_seen[:19].replace("T", " ")) if last_seen else ""
        typer.echo(
            f"{watch_id:<20} | {title_trunc:<35} | {f'{price} ₽':<13} | {f'{max_p} ₽':<10} | {pct_str:<13} | {first_trunc:<19} | {last_trunc:<19}"
        )
    typer.echo(sep)


async def _near_miss(*, summary: bool, since: str | None) -> None:
    db = Database()
    conn = await db.connect()
    watches = {w.id: w for w in load_watchlists()}
    since_dt = parse_near_miss_since(since)
    since_iso = since_dt.isoformat() if since_dt else None

    try:
        if summary:
            await render_near_miss_summary_by_watch(conn, watches, since_iso)
            await render_near_miss_top_10(conn, watches)
        else:
            await render_near_miss_default_table(conn, watches)
    finally:
        await db.close()


@app.command("near-miss")
def near_miss_cmd(
    summary: bool = typer.Option(False,
        '--'
        'summary', "-s", help="Show summary statistics by watch and top 10 closest lots"),
    since: str | None = typer.Option(None, "--since", help="Filter time window, e.g. '24h', '7d'"),
) -> None:
    """List near-miss listings in the headroom band, or show summary statistics."""
    asyncio.run(_near_miss(summary=summary, since=since))
