from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path

import typer
import uvicorn

from hunter.core.config import get_settings, load_watchlists
from hunter.cli_parts.app_01 import (
    app,
    product_app as product_app,
    coverage_app as coverage_app,
    budget_app as budget_app,
    shadow_app as shadow_app,
    run as run,
    check_public_v2_and_run,
    init_hunter_runtime,
    setup_signal_handlers,
    shutdown_hunter_runtime,
    status as status,
    _status as _status,
    pause as pause,
    resume as resume,
    _set_paused as _set_paused,
    explain as explain,
    _explain as _explain,
)
from hunter.cli_parts.eval_05 import (
    backfill as backfill,
    _backfill as _backfill,
    dash as dash,
    eval as eval,
    replay as replay,
    _replay as _replay,
)
from hunter.cli_parts.round2_07 import (
    round2 as round2,
    _round2 as _round2,
    search as search,
    _search as _search,
    sync_catalog as sync_catalog,
    _sync_catalog as _sync_catalog,
    fix_prices as fix_prices,
    _fix_prices as _fix_prices,
    seed_from_legacy_cmd as seed_from_legacy_cmd,
    fetch_budget_cmd_limits_and_night,
)
from hunter.cli_parts.budget_cmd_11 import (
    calc_and_render_watches_budget,
    fetch_and_render_today_db_metrics,
    render_throughput_and_llm_summary,
    search_mode_cmd as search_mode_cmd,
    verify_budget_limits_and_exit,
)
from hunter.cli_parts.search_mode_14 import (
    _search_mode as _search_mode,
    mute_cmd as mute_cmd,
    shadow_report_cmd as shadow_report_cmd,
    unmute_cmd as unmute_cmd,
)
from hunter.cli_parts.near_miss_cmd_18 import (
    _near_miss as _near_miss,
    near_miss_cmd as near_miss_cmd,
)
from hunter.cli_parts.photo_calibrate_cmd_21 import (
    audit_skips_cmd as audit_skips_cmd,
    explain_cmd as explain_cmd,
    jobs_app as jobs_app,
    jobs_list_cmd as jobs_list_cmd,
    jobs_retry_cmd as jobs_retry_cmd,
    photo_calibrate_cmd as photo_calibrate_cmd,
    photo_report_cmd as photo_report_cmd,
    reeval_cmd as reeval_cmd,
)
from hunter.cli_parts.shadow_probe_cmd_24 import (
    cleanup_cmd as cleanup_cmd,
    doctor_cmd as doctor_cmd,
    report_cmd as report_cmd,
    shadow_probe_cmd as shadow_probe_cmd,
    technical_cmd as technical_cmd,
)
from hunter.core.db import get_db
from hunter.core.logging import setup_logging
from hunter.cli_session import import_proxy_credentials, import_slot_session
from hunter.domain.slots import (
    SlotCapacityError, SlotRole, SlotSnapshot, validate_proxy_endpoint, validate_slot_id,
)
from hunter.storage.slot_state import SlotRepository

slot_app = typer.Typer(no_args_is_help=True, help="Manage proxy slots and admission capacity")
app.add_typer(slot_app, name="slot")
slot_app.command("import-session")(import_slot_session)
slot_app.command("import-proxy")(import_proxy_credentials)


def _run_slot_admin[SlotResult](
    operation: Callable[[SlotRepository], Awaitable[SlotResult]],
) -> SlotResult:
    async def _run() -> SlotResult:
        from hunter.bootstrap import Bootstrap

        settings = get_settings().model_copy(deep=True)
        settings.public_v2.enabled = False
        bootstrap = Bootstrap(settings)
        await bootstrap.startup()
        try:
            return await operation(bootstrap.slot_repo)
        finally:
            await bootstrap.shutdown()

    return asyncio.run(_run())


@slot_app.command("add")
def slot_add(
    slot_id: str = typer.Argument(...),
    endpoint: str = typer.Option(..., "--endpoint", envvar="HUNTER_SLOT_ENDPOINT"),
    role: str = typer.Option("active", "--role"),
    account_id: str = typer.Option("", "--account-id"),
) -> None:
    """Register a proxy slot without displaying its endpoint or credentials."""
    try:
        validate_slot_id(slot_id)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="slot_id") from exc
    try:
        validate_proxy_endpoint(endpoint)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="endpoint") from exc
    try:
        slot_role = SlotRole(role)
    except ValueError as exc:
        raise typer.BadParameter("role must be active or reserve") from exc

    async def _add(repo: SlotRepository) -> SlotSnapshot:
        return await repo.register_slot(
            slot_id, endpoint, account_id=account_id, role=slot_role
        )

    slot = _run_slot_admin(_add)
    typer.echo(f"Added slot {slot.slot_id}: role={slot.role.value}, health={slot.health.value}")


@slot_app.command("list")
def slot_list() -> None:
    """List slot health and capacity without printing proxy endpoints."""

    async def _list(repo: SlotRepository) -> list[SlotSnapshot]:
        return await repo.list_slots()

    slots = _run_slot_admin(_list)
    if not slots:
        typer.echo("No proxy slots configured")
        return
    for slot in slots:
        typer.echo(
            f"{slot.slot_id} role={slot.role.value} health={slot.health.value} "
            f"load={slot.capacity.current_load}/{slot.capacity.max_queries}"
        )


@slot_app.command("health")
def slot_health(slot_id: str = typer.Argument(...)) -> None:
    """Show persisted health and quarantine deadline for one slot."""

    async def _health(repo: SlotRepository) -> SlotSnapshot | None:
        return await repo.get_slot(slot_id)

    slot = _run_slot_admin(_health)
    if slot is None:
        typer.echo(f"Slot {slot_id} not found", err=True)
        raise typer.Exit(code=1)
    deadline = slot.quarantined_until or "none"
    typer.echo(
        f"{slot.slot_id} health={slot.health.value} quarantine_until={deadline} "
        f"load={slot.capacity.current_load}/{slot.capacity.max_queries}"
    )


@slot_app.command("admission")
def slot_admission(
    slot_id: str = typer.Argument(...),
    query_id: str = typer.Argument(...),
) -> None:
    """Dry-run assignment admission; this command never writes query state."""

    async def _check(repo: SlotRepository) -> SlotSnapshot:
        return await repo.admission_dry_run(slot_id, query_id)

    try:
        slot = _run_slot_admin(_check)
    except SlotCapacityError as exc:
        typer.echo(f"REJECTED: {exc}")
        raise typer.Exit(code=1) from exc
    typer.echo(
        f"ADMITTED: {slot.slot_id} load={slot.capacity.current_load}/"
        f"{slot.capacity.max_queries}"
    )


@slot_app.command("disable")
def slot_disable(slot_id: str = typer.Argument(...)) -> None:
    """Fence and stop new polls for a slot while preserving assignments and cookies."""

    async def _disable(repo: SlotRepository) -> SlotSnapshot:
        return await repo.disable_slot(slot_id)

    slot = _run_slot_admin(_disable)
    typer.echo(
        f"DISABLED: {slot.slot_id} generation={slot.generation} "
        f"pending_assignments={slot.capacity.current_load}"
    )


@slot_app.command("enable")
def slot_enable(slot_id: str = typer.Argument(...)) -> None:
    """Re-enable a disabled slot in WARMING state without changing cookies."""

    async def _enable(repo: SlotRepository) -> SlotSnapshot:
        return await repo.enable_slot(slot_id)

    slot = _run_slot_admin(_enable)
    typer.echo(f"ENABLED: {slot.slot_id} health={slot.health.value} generation={slot.generation}")


@slot_app.command("retire")
def slot_retire(slot_id: str = typer.Argument(...)) -> None:
    """Fence a slot and transfer queries to reserve slots, reporting degraded leftovers."""

    async def _retire(repo: SlotRepository) -> tuple[list[str], list[str]]:
        return await repo.retire_slot(slot_id)

    transferred, degraded = _run_slot_admin(_retire)
    if degraded:
        typer.echo(
            f"DEGRADED: {slot_id} disabled; transferred={len(transferred)} "
            f"unassigned={','.join(degraded)}"
        )
        raise typer.Exit(code=1)
    typer.echo(f"RETIRED: {slot_id} disabled; transferred={len(transferred)}")


async def _run(*, dash: bool) -> None:
    settings = get_settings()
    setup_logging(settings.data_dir)
    if await check_public_v2_and_run(settings):
        return
    db, browser, notifier, orch, scheduler = await init_hunter_runtime(settings)
    stop = setup_signal_handlers()

    listen_task = asyncio.create_task(notifier.listen(), name="notifier-listen")
    server_task = None
    server = None
    if dash:
        uv_config = uvicorn.Config(
            "hunter.ui.app:create_app",
            factory=True,
            host=settings.ui["host"],
            port=int(settings.ui["port"]),
            log_level="warning",
        )
        server = uvicorn.Server(uv_config)
        server_task = asyncio.create_task(server.serve(), name="dashboard")

    try:
        await stop.wait()
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        await shutdown_hunter_runtime(
            scheduler, orch, listen_task, server, server_task, browser, db
        )


@budget_app.callback(invoke_without_command=True)
def budget_cmd(
    ctx: typer.Context,
    plan: bool = typer.Option(False, "--plan", help="Show budget information"),
) -> None:
    """Request budget and capacity commands."""
    typer.echo("ℹ️ Запросы и лимиты в LotRadar v2 контролируются пулом слотов (hunter slot list) и подписчиками.")


@app.command("price-watch")
def price_watch_cmd(
    json_output: bool = typer.Option(False, "--json", help="Output diagnostic report as raw JSON"),
) -> None:
    """Display price watch coverage, queue age, and secondary request budget."""
    from hunter.application.workload_estimate import estimate_workload
    settings = get_settings()

    async def _do_price_watch() -> None:
        db = get_db()
        await db.connect()
        try:
            plan = await estimate_workload(db._conn, settings.public_v2)
            enabled = bool(getattr(settings.public_v2, "price_watch_enabled", False))
            counts: dict[str, int] = {}
            oldest_due = None
            if hasattr(db, "path") and Path(db.path).exists():
                import aiosqlite
                async with aiosqlite.connect(db.path) as c:
                    cur = await c.execute(
                        'SELECT status, count(*) FROM price_watch_jobs '
                        'GROUP BY status'
                    )
                    counts = {str(r[0]): int(r[1]) for r in await cur.fetchall()}
                    cur2 = await c.execute(
                        'SELECT min(next_run_at) FROM price_watch_jobs '
                        "WHERE status = 'pending'"
                    )
                    row = await cur2.fetchone()
                    oldest_due = row[0] if row else None
            data = {
                "enabled": enabled,
                "status_counts": counts,
                "oldest_due": oldest_due,
                "search_requests_per_day": plan.search_requests_per_day,
                "search_workload_is_estimate": True,
                "extras_estimate_per_day": plan.card_checks_load_per_day if enabled else 0,
            }
            if json_output:
                typer.echo(json.dumps(data, ensure_ascii=False, indent=2))
            else:
                st = "ENABLED" if enabled else "DISABLED (0 tasks/requests)"
                typer.echo(f"Price watch: {st}")
                typer.echo(f"Search workload: {plan.search_requests_per_day} req/day (never mixed with extras)")
                typer.echo(f"Card check extras: {plan.card_checks_load_per_day if enabled else 0} req/day")
                typer.echo(f"Queue: {counts.get('pending', 0)} pending, {counts.get('leased', 0)} leased, {counts.get('retired', 0)} retired")
        finally:
            await db.close()

    asyncio.run(_do_price_watch())


if __name__ == "__main__":
    app()


