from __future__ import annotations

import asyncio
import json
from pathlib import Path
import time
from typing import Any
import uuid

import typer

from hunter.cli_parts.app_01 import app, shadow_app
from hunter.core.db import get_db


def _render_shadow_probe_results(watch: str, variant: str, n: int, results: list[dict[str, Any]]) -> None:
    typer.echo("\n" + "=" * 105)
    typer.echo(f"📊 SPIKE-C SHADOW PROBE REPORT: {watch} ({variant}) — {len(results)}/{n} runs")
    typer.echo("=" * 105)
    typer.echo(f"{'#':<3} | {'Status':<6} | {'Items':<5} | {'Grid':<4} | {'Overlap%':<8} | {'Top20':<5} | {'Price%':<7} | {'Extra':<5} | {'Latency':<8} | {'Block':<10} | {'Chrome RSS'}")
    typer.echo("-" * 105)
    for r in results:
        it = r.get("iteration", 1)
        st = r.get("status", 0)
        items = r.get("n_items", 0)
        grid = "yes" if r.get("has_grid") else "no"
        ov = f"{r.get('id_overlap_pct')}%" if r.get("id_overlap_pct") is not None else "N/A"
        t20 = "yes" if r.get("same_order_top20") == 1 else ("no" if r.get("same_order_top20") == 0 else "N/A")
        pr = f"{r.get('price_equal_pct')}%" if r.get("price_equal_pct") is not None else "N/A"
        extra = r.get("n_extra", 0)
        lat = f"{r.get('latency_ms', 0)}ms"
        blk = r.get("block_marker") or "clean"
        rss = f"{r.get('chrome_rss_after', 0.0):.1f} MB"
        typer.echo(f"{it:<3} | {st:<6} | {items:<5} | {grid:<4} | {ov:<8} | {t20:<5} | {pr:<7} | {extra:<5} | {lat:<8} | {blk:<10} | {rss}")
    typer.echo("=" * 105)


async def _poll_probe_job(db: Any, job_id: int, timeout: int) -> dict[str, Any] | None:
    start_t = time.monotonic()
    while time.monotonic() - start_t < timeout:
        await asyncio.sleep(2.0)
        job = await db.get_job(job_id)
        if job and job.get("status") in ("done", "failed", "poison"):
            return job
    return None


async def _execute_probe_job(watch: str, variant: str, n: int, timeout: int) -> None:
    db = get_db()
    await db.connect()
    dedupe_key = f"probe_{watch}_{variant}_{uuid.uuid4().hex[:8]}"
    payload = {
        "watch_id": watch,
        "variant": variant,
        "n": n,
    }
    job_id = await db.enqueue_job(
        kind="shadow_probe",
        product_id=watch,
        dedupe_key=dedupe_key,
        payload=payload,
        priority=10,
    )
    if not job_id:
        typer.echo("Error: failed to enqueue shadow_probe job.", err=True)
        await db.close()
        raise typer.Exit(code=1)

    typer.echo(f"Enqueued shadow_probe job #{job_id} for watch '{watch}' (variant={variant}, n={n}).")
    typer.echo("Waiting for service to execute probe in background...")

    completed_job = await _poll_probe_job(db, job_id, timeout)
    await db.close()

    if not completed_job:
        typer.echo(f"Error: probe job #{job_id} timed out after {timeout}s.", err=True)
        raise typer.Exit(code=1)

    if completed_job.get("status") != "done":
        typer.echo(f"Error: probe job #{job_id} failed with status '{completed_job.get('status')}': {completed_job.get('last_error')}", err=True)
        raise typer.Exit(code=1)

    raw_payload = completed_job["payload"]
    res_payload = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
    if res_payload.get("status") == "failed":
        typer.echo(f"Probe failed: {res_payload.get('error')}", err=True)
        raise typer.Exit(code=1)

    results = res_payload.get("results", [])
    _render_shadow_probe_results(watch, variant, n, results)


@shadow_app.command("probe")
def shadow_probe_cmd(
    watch: str = typer.Option("gem_headphones_sony", "--watch", help="Watch ID to probe"),
    variant: str = typer.Option("fetch", "--variant", help="Variant to test: fetch or light_nav"),
    n: int = typer.Option(3, "--n", help="Number of probe requests (default: 3)"),
    timeout: int = typer.Option(360, "--timeout", help="Max seconds to wait for probe to complete"),
) -> None:
    """Run an isolated shadow probe through the running service's jobs queue."""
    if variant not in ("fetch", "light_nav"):
        typer.echo(f"Error: variant must be 'fetch' or 'light_nav', got '{variant}'", err=True)
        raise typer.Exit(code=1)

    asyncio.run(_execute_probe_job(watch, variant, n, timeout))


@app.command("technical")
def technical_cmd(
    proxy: str | None = typer.Option(None, "--proxy", help="Optional proxy URL to test (e.g. http://user:pass@host:port)"),
) -> None:
    """Safe pre-flight check of session health and trust level via /info/show_technical."""
    from hunter.collect.technical import check_technical_session

    async def _do_check() -> None:
        info = await check_technical_session(proxy=proxy)
        typer.echo("=" * 60)
        typer.echo("Avito Technical Session Diagnostics (/info/show_technical)")
        typer.echo("=" * 60)
        typer.echo(f"  IP Address:    {info.ip or 'Unknown'}")
        typer.echo(f"  Trust Level:   {info.trust_level or 'Unknown'}")
        typer.echo(f"  City:          {info.city or 'Unknown'}")
        typer.echo(f"  Region:        {info.region or 'Unknown'}")
        typer.echo(f"  Cookies:       {'Включены' if info.cookies_enabled is True else ('Отключены' if info.cookies_enabled is False else 'Unknown')}")
        typer.echo(f"  Server Node:   {info.server or 'Unknown'}")
        typer.echo(f"  Trusted:       {'YES' if info.is_trusted else 'NO (DEGRADED)'}")
        typer.echo("=" * 60)

    asyncio.run(_do_check())


@app.command("doctor")
def doctor_cmd(
    json_output: bool = typer.Option(False, "--json", help="Output diagnostic report as raw JSON"),
) -> None:
    """Run full self-diagnostic health checks on local database, disk, backups, and budget."""
    from hunter.system.doctor import run_diagnostics

    async def _do_doctor() -> None:
        db = get_db()
        await db.connect()
        try:
            report = await run_diagnostics(db=db)
            if json_output:
                from dataclasses import asdict
                typer.echo(json.dumps(asdict(report), ensure_ascii=False, indent=2))
            else:
                typer.echo(report.format_text())
        finally:
            await db.close()

    asyncio.run(_do_doctor())


@app.command("report")
def report_cmd(
    date: str | None = typer.Option(None, "--date", help=        'Target date in YYYY-MM-DD format (defaults to '
        'today)'),
    out_file: str | None = typer.Option(None, "--out", help=(
        'Optional custom output path for markdown file'
    )),
) -> None:
    """Generate compact daily markdown report into data/reports/ (without sending to owner)."""
    from hunter.system.report import generate_daily_report

    async def _do_report() -> None:
        db = get_db()
        await db.connect()
        try:
            out_p = Path(out_file) if out_file else None
            saved = await generate_daily_report(db=db, target_date=date, reports_dir=out_p.parent if out_p else None)
            typer.echo(f"Daily report generated: {saved}")
        finally:
            await db.close()

    asyncio.run(_do_report())


@app.command("cleanup")
def cleanup_cmd(
    job_days: int = typer.Option(30, "--jobs", help=(
        'Retention days for completed jobs (default: 30)'
    )),
    log_days: int = typer.Option(14, "--logs", help=(
        'Retention days for ephemeral debug & memory logs '
        '(default: 14)'
    )),
) -> None:
    (
        'Safely prune historical completed jobs and aged '
        'debug logs without touching listings or alerts.'
    )
    async def _do_cleanup() -> None:
        db = get_db()
        await db.connect()
        try:
            stats = await db.cleanup_old_records(
                job_retention_days=job_days,
                request_log_retention_days=log_days,
                memory_log_retention_days=log_days,
                events_retention_days=job_days,
            )
            typer.echo(f"Cleanup completed: {stats}")
        finally:
            await db.close()

    asyncio.run(_do_cleanup())

