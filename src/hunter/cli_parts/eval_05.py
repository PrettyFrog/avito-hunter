"""Evaluation and replay commands (deprecated in v2)."""

from __future__ import annotations

import typer

from hunter.cli_parts.app_01 import app


@app.command()
def eval(  # noqa: A001
    title: str = typer.Option(..., help="listing title"),
    price: int = typer.Option(..., help="asking price"),
    watch: str = typer.Option(..., help="watchlist id"),
    description: str = typer.Option("", help="optional description"),
) -> None:
    """Run Round 1 on a title/price without touching the site."""
    typer.echo("ℹ️ Команда eval устарела в LotRadar v2.")


@app.command()
def dash() -> None:
    """Local dashboard."""
    typer.echo("ℹ️ Веб-дашборд устарел в LotRadar v2. Мониторинг осуществляется через Telegram-бота.")


@app.command()
def replay(path: str | None = typer.Argument(None)) -> None:
    """Re-parse a saved gzip HTML card (no network)."""
    typer.echo("ℹ️ Команда replay устарела в LotRadar v2.")


async def _replay(path: str | None) -> None:
    pass


@app.command()
def backfill(
    dry_run: bool = typer.Option(False, "--dry-run", help="Do not write results to DB"),
    diff: bool = typer.Option(False, "--diff", help="Compare legacy matching verdicts"),
) -> None:
    """Re-run Round 1 on saved card HTML without fetching Avito."""
    typer.echo("ℹ️ Команда backfill устарела в LotRadar v2.")


async def _backfill(dry_run: bool, diff: bool) -> None:
    pass
