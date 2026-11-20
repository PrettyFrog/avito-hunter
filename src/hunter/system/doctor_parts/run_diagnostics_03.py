from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hunter.core.config import Settings
    from hunter.core.db import Database
    from hunter.system.doctor import DiagnosticCheck


async def check_request_budget(
    db: Database | None,
    settings: Settings,
) -> DiagnosticCheck | None:
    from hunter.system.doctor import DiagnosticCheck

    if not db:
        return None
    try:
        today_reqs = await db.today_requests()
        max_budget = getattr(settings.request_budget, "max_per_day", 900)
        pct = round((today_reqs / max_budget) * 100, 1) if max_budget else 0.0
        if pct >= 95.0:
            return DiagnosticCheck(
                name="request_budget",
                status="warn",
                message=f"Budget near ceiling: {today_reqs}/{max_budget} requests ({pct}%)",
            )
        return DiagnosticCheck(
            name="request_budget",
            status="ok",
            message=f"{today_reqs}/{max_budget} requests used today ({pct}%)",
        )
    except Exception as exc:
        return DiagnosticCheck(
            name="request_budget", status="warn", message=f"Budget check error: {exc}"
        )


def check_process_memory() -> DiagnosticCheck:
    from hunter.system.doctor import DiagnosticCheck
    from hunter.system.memory import get_process_rss_mb

    try:
        rss = get_process_rss_mb()
        if rss > 1000.0:
            return DiagnosticCheck(
                name="process_memory",
                status="warn",
                message=f"High process RSS: {rss} MB (ceiling 1.1 GB)",
            )
        return DiagnosticCheck(
            name="process_memory",
            status="ok",
            message=f"Process memory healthy ({rss} MB RSS)",
        )
    except Exception as exc:
        return DiagnosticCheck(
            name="process_memory", status="warn", message=f"Memory check error: {exc}"
        )


def check_products_health(settings: Settings) -> DiagnosticCheck:
    from hunter.core.product import get_product_store
    from hunter.system.doctor import DiagnosticCheck

    try:
        store = get_product_store(settings.data_dir / "products")
        prods_dict, errors = store.load_all()
        prods = list(prods_dict.values())
        live_count = sum(1 for p in prods if p.mode == "live")
        if errors:
            return DiagnosticCheck(
                name="products_health",
                status="warn",
                message=(
                    f"{len(prods)} products loaded, but "
                    + f"{len(errors)} validation errors detected"
                ),
                details={"errors": errors, "profiles": [p.id for p in prods]},
            )
        return DiagnosticCheck(
            name="products_health",
            status="ok",
            message=f"{len(prods)} product profiles loaded ({live_count} live)",
            details={"profiles": [p.id for p in prods]},
        )
    except Exception as exc:
        return DiagnosticCheck(
            name="products_health", status="warn", message=f"Product store check: {exc}"
        )
