from __future__ import annotations

import asyncio
from datetime import datetime
import json
from pathlib import Path
import sqlite3
from typing import Any

import typer

from hunter.cli_parts.app_01 import app
from hunter.core.config import load_watchlists
from hunter.core.db import get_db


@app.command()
def round2(avito_id: str) -> None:
    """Run full Round 2 analysis (Vision, OCR, LLM, Judge) on a listing."""
    asyncio.run(_round2(avito_id))


async def _round2(avito_id: str) -> None:
    typer.echo("ℹ️ Команда round2 устарела в LotRadar v2.")


@app.command()
def search(query: str, limit: int = 20) -> None:
    """Full-text search in collected listings (FTS5)."""
    asyncio.run(_search(query, limit))


async def _search(query: str, limit: int) -> None:
    db = get_db()
    await db.connect()
    results = await db.search_listings_fts(query, limit=limit)
    if not results:
        typer.echo("no listings matched")
    for r in results:
        typer.echo(f"{r['avito_id']} | {r.get('price')} ₽ | {r.get('title')}")
    await db.close()


@app.command(name="sync-catalog")
def sync_catalog() -> None:
    """Sync YAML catalogs into SQLite products table."""
    asyncio.run(_sync_catalog())


async def _sync_catalog() -> None:
    typer.echo("ℹ️ Каталоги моделей v1 устарели в LotRadar v2.")


@app.command(name="fix-prices")
def fix_prices(
    dry_run: bool = typer.Option(True,
        '--dry-run/--no-dry-'
        'run', help="Report candidate listings without modifying"),
    threshold: int = typer.Option(1_000_000, help="Threshold above which price is suspect"),
) -> None:
    """Find and fix implausible listing prices (> 1,000,000 RUB)."""
    asyncio.run(_fix_prices(dry_run=dry_run, threshold=threshold))


async def _fix_prices(*, dry_run: bool, threshold: int) -> None:
    db = get_db()
    await db.connect()
    try:
        suspects = await db.find_implausible_prices(threshold=threshold)
        if not suspects:
            typer.echo("No implausible prices found (> 1,000,000).")
            return
        typer.echo(f"Found {len(suspects)} listing(s) with price > {threshold:,}:")
        for row in suspects:
            typer.echo(f"  - {row['avito_id']} | {row['price']:,} ₽ | {row.get('title') or ''}")
        if dry_run:
            typer.echo(f"dry-run: {len(suspects)} listings would be set to price=NULL, price_suspect=1")
        else:
            updated = await db.fix_implausible_prices(threshold=threshold)
            typer.echo(f"Updated {updated} listings: price=NULL, price_suspect=1")
    finally:
        await db.close()


@app.command(name="seed-from-legacy")
def seed_from_legacy_cmd(
    old: Path = typer.Option(..., "--old", help="Path to legacy database (opened mode=ro)"),
    new: Path = typer.Option(..., "--new", help="Path to new database to seed"),
    dry_run: bool = typer.Option(False,
        '--dry-'
        'run', help="Preview counts without writing to new DB"),
) -> None:
    """Migrate memory from legacy DB to fresh DB."""
    from hunter.core.seed import seed_from_legacy

    counts = asyncio.run(seed_from_legacy(old_path=old, new_path=new, dry_run=dry_run))
    prefix = "[DRY-RUN] Would transfer:" if dry_run else "Successfully seeded:"
    typer.echo(f"{prefix} {counts}")


def fetch_budget_cmd_limits_and_night(settings: Any, db: Any) -> tuple[int, int, int, float]:
    if isinstance(settings.budget, dict):
        limit = int(settings.budget.get(
            'daily_'
            'cap', getattr(settings.request_budget, "daily_cap", 10000)))
    else:
        limit = int(getattr(settings.budget,
            'daily_'
            'cap', getattr(settings.request_budget, "daily_cap", 10000)))
    night_enabled_db = None
    try:
        if db.path.exists():
            con = sqlite3.connect(f"file:{db.path}?mode=ro", uri=True)
            cur = con.cursor()
            cur.execute("SELECT value FROM runtime WHERE key = 'budget_daily_cap'")
            row = cur.fetchone()
            if row and row[0]:
                limit = int(row[0])
            cur.execute("SELECT value FROM runtime WHERE key = 'budget_night_enabled'")
            row_n = cur.fetchone()
            if row_n and row_n[0]:
                night_enabled_db = row_n[0] in ("1", "true", "True")
            con.close()
    except Exception:
        pass

    budget_cfg = settings.request_budget
    night_enabled = night_enabled_db if night_enabled_db is not None else getattr(budget_cfg, "night_enabled", True)
    if not night_enabled:
        night_hours = 0
        day_hours = 24
    else:
        night_start, night_end = budget_cfg.night_hours
        night_hours = (night_end - night_start) % 24
        day_hours = 24 - night_hours

    card_ratio = 0.05
    try:
        if db.path.exists():
            con = sqlite3.connect(f"file:{db.path}?mode=ro", uri=True)
            cur = con.cursor()
            cur.execute("SELECT sum(requests), sum(hits_total) FROM metrics")
            row = cur.fetchone()
            if row and row[0] and row[1] and row[1] > 0:
                reqs, hits = row[0], row[1]
                if reqs > hits:
                    card_ratio = min(0.25, max(0.02, (reqs - hits) / hits))
            con.close()
    except Exception:
        pass
    return limit, day_hours, night_hours, card_ratio


def fetch_budget_cmd_history_7d(db: Any) -> tuple[dict[str, int], int, int, float, str]:
    fact_7d: dict[str, int] = {}
    total_fact_7d = 0
    days_logged = 7
    history_hours = 0.0
    history_header = "Факт 7д"
    try:
        if db.path.exists():
            con = sqlite3.connect(f"file:{db.path}?mode=ro", uri=True)
            cur = con.cursor()
            table_check = cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND "
                "name='request_log'").fetchone()
            if table_check:
                rows_7d = cur.execute("""
                    SELECT product_id, COUNT(*)
                    FROM request_log
                    WHERE datetime(ts) >= datetime('now', '-7 days')
                      AND req_type != 'photo'
                    GROUP BY product_id
                """).fetchall()
                for pid, cnt in rows_7d:
                    if pid:
                        fact_7d[pid] = cnt
                        total_fact_7d += cnt
                time_range = cur.execute("""
                    SELECT MIN(ts), MAX(ts), COUNT(DISTINCT date(ts))
                    FROM request_log
                    WHERE datetime(ts) >= datetime('now', '-7 days')
                      AND req_type != 'photo'
                """).fetchone()
                if time_range and time_range[0] and time_range[1]:
                    try:
                        t_min = datetime.fromisoformat(time_range[0])
                        t_max = datetime.fromisoformat(time_range[1])
                        history_hours = max(0.1, (t_max - t_min).total_seconds() / 3600.0)
                    except Exception:
                        history_hours = 0.0
                    days_cnt = time_range[2] or 1
                    days_logged = max(1, days_cnt)
                    if history_hours > 0 and history_hours < 24.0:
                        history_header = f"Факт ({history_hours:.0f}ч)"
                    elif days_logged > 1:
                        history_header = f"Факт ({days_logged}д)"
                    else:
                        history_header = "Факт 7д"
            con.close()
    except Exception:
        pass
    return fact_7d, total_fact_7d, days_logged, history_hours, history_header

