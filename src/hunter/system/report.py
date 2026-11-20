from __future__ import annotations

from pathlib import Path

from hunter.core.config import get_settings
from hunter.core.db import Database
from hunter.core.models import now_utc
from hunter.system.report_parts.generate_daily_report_01 import (
    build_executive_summary_section,
    fetch_daily_evaluation_stats,
    fetch_daily_metrics_and_storage,
    fetch_daily_notification_stats,
    format_report_breakdown_tables,
)


async def generate_daily_report(
    db: Database,
    target_date: str | None = None,
    reports_dir: Path | None = None,
) -> Path:
    """Generates a compact daily summary markdown report and saves it to data/reports/."""
    settings = get_settings()
    out_dir = reports_dir or (settings.data_dir / "reports")
    out_dir.mkdir(parents=True, exist_ok=True)

    date_str = target_date or now_utc().strftime("%Y-%m-%d")
    out_file = out_dir / f"daily_{date_str}.md"

    conn = await db.connect()

    # 1. Notifications Sent Today
    notif_rows, total_notifications = await fetch_daily_notification_stats(conn, date_str)

    # 2 & 3. Evaluations Breakdown & Top Skip Reasons
    eval_rows, total_evals, skip_rows = await fetch_daily_evaluation_stats(conn, date_str)

    # 4 & 5. Metrics & Database Footprint
    metrics_dict, db_size_mb, wal_size_mb = await fetch_daily_metrics_and_storage(
        conn, date_str, settings.db_path
    )

    lines = await build_executive_summary_section(
        conn, date_str, total_notifications, total_evals, metrics_dict, db_size_mb, wal_size_mb
    )
    lines.extend(format_report_breakdown_tables(notif_rows, eval_rows, skip_rows, total_evals))

    content = "\n".join(lines) + "\n"
    out_file.write_text(content, encoding="utf-8")
    return out_file
