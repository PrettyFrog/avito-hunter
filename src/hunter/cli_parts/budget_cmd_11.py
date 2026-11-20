from __future__ import annotations

import sqlite3
from typing import Any
import typer

from hunter.cli_parts.app_01 import app
from hunter.cli_parts.round2_07 import fetch_budget_cmd_history_7d
from hunter.cli_parts.budget_cmd_09 import (
    build_watches_budget_rows,
    render_watches_budget_table,
    render_worst_case_and_headroom,
)


def _render_actual_requests_log(cur: sqlite3.Cursor) -> None:
    log_check = cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='request_log'"
    ).fetchone()
    if not log_check:
        return
    actual_rows = cur.execute("""
        SELECT product_id,
               SUM(CASE WHEN req_type IN ('search', 'search_light') THEN 1 ELSE 0 END) as sl_cnt,
               SUM(CASE WHEN req_type = 'search_render' THEN 1 ELSE 0 END) as sr_cnt,
               SUM(CASE WHEN req_type = 'card' THEN 1 ELSE 0 END) as c_cnt,
               SUM(CASE WHEN req_type = 'photo' THEN 1 ELSE 0 END) as p_cnt,
               COUNT(*) as tot_cnt
        FROM request_log
        WHERE date(ts) = date('now')
        GROUP BY product_id
        ORDER BY product_id
    """).fetchall()
    if not actual_rows:
        return
    typer.echo("\nФактический расход за сегодня (request_log):")
    act_header = (
        f"{'Товар (ID)':<32} | {'Light':<8} | {'Render':<8} | "
        f"{'Карточки':<10} | {'Фото':<6} | {'Итого':<8}"
    )
    typer.echo(act_header)
    typer.echo("-" * len(act_header))
    for a_row in actual_rows:
        typer.echo(
            f"{a_row[0]:<32} | {a_row[1]:<8} | {a_row[2]:<8} | "
            f"{a_row[3]:<10} | {a_row[4]:<6} | {a_row[5]:<8}"
        )


def fetch_and_render_today_db_metrics(db: Any) -> list[dict[str, Any]]:
    llm_stats: list[dict[str, Any]] = []
    try:
        if db.path.exists():
            con = sqlite3.connect(f"file:{db.path}?mode=ro", uri=True)
            cur = con.cursor()
            table_check = cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='llm_calls'"
            ).fetchone()
            if table_check:
                llm_rows = cur.execute("""
                    SELECT provider, model,
                           COUNT(*) as total_calls,
                           SUM(CASE WHEN status = '429' OR error_msg LIKE '%429%'
                                    OR error_msg LIKE '%RESOURCE_EXHAUSTED%'
                                    THEN 1 ELSE 0 END) as cnt_429,
                           SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END) as cnt_ok,
                           COALESCE(SUM(tokens_total), 0) as tot_tokens,
                           COALESCE(AVG(latency_ms), 0) as avg_lat
                    FROM llm_calls
                    WHERE date(ts) = date('now')
                    GROUP BY provider, model
                    ORDER BY provider, model
                """).fetchall()
                for prov, mod, tot, c429, cok, tok, lat in llm_rows:
                    ratio_429 = (c429 / tot) if tot > 0 else 0.0
                    llm_stats.append(
                        {
                            "provider": prov,
                            "model": mod,
                            "total": tot,
                            "cnt_429": c429,
                            "cnt_ok": cok,
                            "tokens": tok,
                            "latency": lat,
                            "ratio_429": ratio_429,
                        }
                    )

            _render_actual_requests_log(cur)
            con.close()
    except Exception:
        pass
    return llm_stats


def calc_and_render_watches_budget(
    settings: Any,
    db: Any,
    watches: list[Any],
    limit: int,
    day_hours: int,
    night_hours: int,
    card_ratio: float,
) -> tuple[list[dict[str, Any]], float, float]:
    intervals = getattr(settings, "priority_intervals", None) or {
        "high": 15,
        "normal": 30,
        "low": 60,
    }
    fact_7d, total_fact_7d, days_logged, history_hours, history_header = (
        fetch_budget_cmd_history_7d(db)
    )

    (
        rows,
        tot_sph_day,
        tot_sph_night,
        tot_searches_day,
        tot_cards_day,
        tot_photos_day,
        tot_daily_reqs,
    ) = build_watches_budget_rows(
        watches, intervals, day_hours, night_hours, card_ratio, fact_7d, history_hours, days_logged
    )
    daily_fact_total, sep = render_watches_budget_table(
        rows,
        history_header,
        tot_sph_day,
        history_hours,
        total_fact_7d,
        days_logged,
        tot_searches_day,
        tot_cards_day,
        tot_photos_day,
        tot_daily_reqs,
    )
    tot_cards_day, tot_daily_reqs, worst_case_reqs = render_worst_case_and_headroom(
        settings,
        watches,
        limit,
        tot_searches_day,
        tot_cards_day,
        tot_photos_day,
        tot_daily_reqs,
        sep,
        history_hours,
        total_fact_7d,
        days_logged,
        daily_fact_total,
    )
    return rows, tot_daily_reqs, worst_case_reqs


def render_llm_calls_summary(llm_stats: list[dict[str, Any]]) -> None:
    gemini_stat = next((s for s in llm_stats if s["provider"] == "gemini"), None)
    if gemini_stat:
        typer.echo(
            f"Gemini quota: {gemini_stat['total']} calls today "
            f"({gemini_stat['cnt_429']} x 429), status: "
            f"{'WARNING' if gemini_stat['ratio_429'] > 0.2 else 'OK'}"
        )
    else:
        typer.echo("Gemini quota / LLM calls: 0 calls recorded today (status: OK)")
    if llm_stats:
        typer.echo("LLM / Vision вызовы за сегодня (llm_calls):")
        for st in llm_stats:
            warn_str = (
                f" ⚠️ 429 ratio > 20% ({st['ratio_429']:.1%})" if st["ratio_429"] > 0.20 else ""
            )
            typer.echo(
                f"  - {st['provider']} ({st['model']}): {st['total']} вызовов "
                f"({st['cnt_ok']} ok, {st['cnt_429']} x 429 [{st['ratio_429']:.1%}], "
                f"{st['tokens']:,} токенов, ~{st['latency']:.0f}мс){warn_str}".replace(",", " ")
            )
            if st["ratio_429"] > 0.20:
                typer.secho(
                    f"⚠️ ВНИМАНИЕ: Доля 429 ошибок для {st['provider']} ({st['model']}) "
                    f"превышает 20% ({st['ratio_429']:.1%})! Лимит квоты исчерпывается.",
                    fg=typer.colors.YELLOW,
                    bold=True,
                )
    else:
        typer.echo("LLM / Vision вызовы за сегодня: 0 вызовов зарегистрировано")


def render_throughput_and_llm_summary(
    settings: Any,
    limit: int,
    day_hours: int,
    night_hours: int,
    card_ratio: float,
    rows: list[dict[str, Any]],
    llm_stats: list[dict[str, Any]],
    worst_retries: float = 15.0,
    shadow_note: str | None = None,
) -> None:
    if shadow_note is None:
        shadow_cfg = getattr(settings, "shadow", {}) or {}
        max_shadow_cards_cfg = shadow_cfg.get("max_cards_per_day", 40)
        if isinstance(max_shadow_cards_cfg, dict):
            model_cap = int(max_shadow_cards_cfg.get("model_watches", 80))
            gem_cap = int(max_shadow_cards_cfg.get("gem", 15))
            shadow_note = (
                f", shadow cards cap: model={model_cap}, "
                f"gem={gem_cap} (total={model_cap + gem_cap})"
            )
        else:
            shadow_note = f", shadow cards cap: {int(max_shadow_cards_cfg)}"

    gem_cards_day = sum(r["cards_day"] for r in rows if "gem" in r["id"])
    est_vision_calls = gem_cards_day * 2.0

    collect_cfg = getattr(settings, "collect", None)
    light_min = float(getattr(collect_cfg, "light_min_delay_seconds", None) or 3.0)
    light_max = float(getattr(collect_cfg, "light_max_delay_seconds", None) or 6.0)
    light_avg = (light_min + light_max) / 2.0
    budget_cfg = settings.request_budget
    render_avg = (float(budget_cfg.min_delay_seconds) + float(budget_cfg.max_delay_seconds)) / 2.0
    night_mult = (budget_cfg.night_multiplier or 1.8) if night_hours > 0 else 1.0
    light_cap = int(
        (day_hours * 3600 / light_avg) + (night_hours * 3600 / (light_avg * night_mult))
    )
    render_cap = int(
        (day_hours * 3600 / render_avg) + (night_hours * 3600 / (render_avg * night_mult))
    )
    light_pct = (limit / light_cap * 100.0) if light_cap > 0 else 0.0

    schedule_desc = "night 01-07 MSK 2x slower" if night_hours > 0 else "night slowdown disabled"
    typer.echo(
        f"Daily budget limit: {limit} req/day ({schedule_desc}, card ratio: "
        f"{card_ratio:.1%}, worst-case retries: {int(worst_retries)}{shadow_note})"
    )
    typer.echo(
        f"Потолок пропускной способности при текущих паузах: light ~{light_cap}/сут, "
        f"render ~{render_cap}/сут (daily_cap {limit}: {light_pct:.1f}% от потолка light)"
    )
    typer.echo(
        "Примечание по времени: Limiter считает по суткам UTC "
        "(00:00 UTC = 03:00 МСК), выключатель и бюджет — по скользящим 24 часам."
    )
    typer.echo(f"Vision calls: expected ~{est_vision_calls:.1f}/day across gem feeds")
    render_llm_calls_summary(llm_stats)


def verify_budget_limits_and_exit(
    worst_case_reqs: float, tot_daily_reqs: float, limit: int
) -> None:
    if worst_case_reqs > limit:
        typer.secho(
            f"ERROR: Worst-case daily requests ({worst_case_reqs:.1f}) EXCEEDS "
            f"daily budget limit ({limit}) by {worst_case_reqs - limit:.1f}!",
            fg=typer.colors.RED,
            bold=True,
        )
        raise typer.Exit(code=1)
    elif tot_daily_reqs > limit:
        typer.secho(
            f"ERROR: Expected daily requests ({tot_daily_reqs:.1f}) EXCEEDS "
            f"daily budget limit ({limit}) by {tot_daily_reqs - limit:.1f}!",
            fg=typer.colors.RED,
            bold=True,
        )
        raise typer.Exit(code=1)
    else:
        typer.secho(
            f"OK: Expected daily requests ({tot_daily_reqs:.1f}) and worst-case "
            f"({worst_case_reqs:.1f}) are within limit ({limit}). "
            f"Margin: {limit - worst_case_reqs:.1f} requests.",
            fg=typer.colors.GREEN,
            bold=True,
        )


@app.command(name="search-mode")
def search_mode_cmd(
    action: str = typer.Argument("status", help="Action: 'status', 'light', or 'render'"),
) -> None:
    """Inspect or control SERP retrieval mode (light in-tab fetch vs full render)."""
    import asyncio
    from hunter import cli

    asyncio.run(cli._search_mode(action))
