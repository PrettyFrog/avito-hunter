from __future__ import annotations

from pathlib import Path
from typing import Any


async def fetch_daily_notification_stats(conn: Any, date_str: str) -> tuple[list[Any], int]:
    cur = await conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('notifications', 'deliveries')")
    tables = {r[0] for r in await cur.fetchall()}
    if "notifications" in tables:
        cur_notif = await conn.execute(
            """
            SELECT l.watchlist_id, count(*) as count, count(DISTINCT n.avito_id) as unique_lots
            FROM notifications n
            JOIN listings l ON n.avito_id = l.avito_id
            WHERE n.created_at LIKE ?
            GROUP BY l.watchlist_id
            ORDER BY count DESC
            """,
            (f"{date_str}%",),
        )
        notif_rows = await cur_notif.fetchall()
        return notif_rows, sum(r[1] for r in notif_rows)
    if "deliveries" in tables:
        cur_notif = await conn.execute(
            """
            SELECT COALESCE(l.watchlist_id, 'v2_search') as watchlist_id, count(*) as count, count(DISTINCT e.avito_id) as unique_lots
            FROM deliveries d
            JOIN events e ON d.event_id = e.id
            LEFT JOIN listings l ON e.avito_id = l.avito_id
            WHERE d.created_at LIKE ? AND d.status = 'sent'
            GROUP BY watchlist_id
            ORDER BY count DESC
            """,
            (f"{date_str}%",),
        )
        notif_rows = await cur_notif.fetchall()
        return notif_rows, sum(r[1] for r in notif_rows)
    return [], 0


async def fetch_daily_evaluation_stats(
    conn: Any,
    date_str: str,
) -> tuple[list[Any], int, list[Any]]:
    cur = await conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='evaluations'")
    if not await cur.fetchone():
        return [], 0, []
    cur_eval = await conn.execute(
        """
        SELECT verdict, count(*) as count
        FROM evaluations
        WHERE created_at LIKE ?
        GROUP BY verdict
        ORDER BY count DESC
        """,
        (f"{date_str}%",),
    )
    eval_rows = await cur_eval.fetchall()
    total_evals = sum(r[1] for r in eval_rows)

    cur_skips = await conn.execute(
        """
        SELECT skip_reason, count(*) as count
        FROM evaluations
        WHERE created_at LIKE ? AND skip_reason IS NOT NULL AND skip_reason != ''
        GROUP BY skip_reason
        ORDER BY count DESC
        LIMIT 10
        """,
        (f"{date_str}%",),
    )
    skip_rows = await cur_skips.fetchall()
    return eval_rows, total_evals, skip_rows


async def fetch_daily_metrics_and_storage(
    conn: Any,
    date_str: str,
    db_path: Path | str,
) -> tuple[dict[str, Any], float, float]:
    metrics_dict: dict[str, Any] = {}
    try:
        cur_metrics = await conn.execute(
            """
            SELECT *
            FROM metrics
            WHERE day = ?
            """,
            (date_str,),
        )
        metric_row = await cur_metrics.fetchone()
        if metric_row:
            metrics_dict = dict(metric_row)
    except Exception:
        pass

    cur_pages = await conn.execute("PRAGMA page_count;")
    pages = (await cur_pages.fetchone())[0]
    cur_psize = await conn.execute("PRAGMA page_size;")
    psize = (await cur_psize.fetchone())[0]
    db_size_mb = round((pages * psize) / (1024 * 1024), 2)

    wal_path = Path(str(db_path) + "-wal")
    wal_size_mb = round(wal_path.stat().st_size / (1024 * 1024), 2) if wal_path.exists() else 0.0
    return metrics_dict, db_size_mb, wal_size_mb



async def build_executive_summary_section(
    conn: Any,
    date_str: str,
    total_notifications: int,
    total_evals: int,
    metrics_dict: dict[str, Any],
    db_size_mb: float,
    wal_size_mb: float,
) -> list[str]:
    from hunter.core.models import now_utc

    lines = [
        f"# Daily Hunter Report — {date_str}",
        f"*Generated at: {now_utc().isoformat()}*",
        "",
        "## 1. Executive Summary",
        f"- **Total Notifications Sent**: {total_notifications}",
        f"- **Listings Evaluated**: {total_evals}",
        f"- **Search Requests (Metrics)**: {metrics_dict.get('parse_ok', 0)} ok / {metrics_dict.get('empty_ok', 0)} empty",
        f"- **HTTP 429 Errors**: {metrics_dict.get('429', 0)}",
        f"- **HTTP 403 / Captchas**: {metrics_dict.get('403', 0)} / {metrics_dict.get('captcha', 0)}",
        f"- **Poison Jobs**: {metrics_dict.get('poison_jobs', 0)}",
        f"- **Database Footprint**: {db_size_mb} MB (WAL: {wal_size_mb} MB)",
    ]

    try:
        from hunter.storage.read_models import fetch_system_health
        health = await fetch_system_health(conn)
        lines.append(f"- **Health (Active/Degraded/Bootstrap)**: {health.active_queries} / {health.degraded_queries} / {health.bootstrap_queries}")
        oldest = f"{health.oldest_pending_delivery_age_sec}s" if health.oldest_pending_delivery_age_sec is not None else "none"
        lag = f"{health.slot_schedule_lag_sec}s" if health.slot_schedule_lag_sec is not None else "unknown"
        lines.append(f"- **Oldest Delivery / Slot Lag**: {oldest} / {lag}")
    except Exception:
        lines.append("- **Health**: unavailable")

    try:
        from hunter.storage.read_models import fetch_query_economics
        econ = await fetch_query_economics(conn, is_owner=True)
        lines.append(f"- **Economics (S/Q/Slots)**: {econ.logical_subscriptions} subs / {econ.physical_queries} physical / {econ.active_slots}+{econ.reserve_slots} slots")
        lines.append(f"- **Cost Estimate (Saved)**: {econ.total_monthly_cost:.0f} ₽ ({econ.cost_savings:.0f} ₽ saved, {econ.request_savings_pct}%)")
    except Exception:
        lines.append("- **Economics**: unavailable")

    return lines


def format_report_breakdown_tables(
    notif_rows: list[Any],
    eval_rows: list[Any],
    skip_rows: list[Any],
    total_evals: int,
) -> list[str]:
    lines = [
        "",
        "## 2. Notifications by Watchlist",
    ]

    if notif_rows:
        lines.append("| Watchlist | Total Sent | Unique Lots |")
        lines.append("|---|---|---|")
        for row in notif_rows:
            lines.append(f"| `{row[0]}` | {row[1]} | {row[2]} |")
    else:
        lines.append("*No notifications were triggered for this date.*")

    lines.append("")
    lines.append("## 3. Evaluation Verdicts")
    if eval_rows:
        lines.append("| Verdict | Count | Ratio |")
        lines.append("|---|---|---|")
        for row in eval_rows:
            pct = round((row[1] / total_evals) * 100, 1) if total_evals else 0.0
            lines.append(f"| `{row[0]}` | {row[1]} | {pct}% |")
    else:
        lines.append("*No evaluations recorded.*")

    lines.append("")
    lines.append("## 4. Top Skip Reasons")
    if skip_rows:
        lines.append("| Reason | Count |")
        lines.append("|---|---|")
        for row in skip_rows:
            lines.append(f"| `{row[0]}` | {row[1]} |")
    else:
        lines.append("*No skips recorded.*")

    lines.append("")
    lines.append("---")
    lines.append("*This report is generated locally for maintenance without broadcasting to owners.*")
    return lines
