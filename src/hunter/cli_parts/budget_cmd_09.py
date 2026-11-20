from __future__ import annotations

from typing import Any
import typer


def calc_watch_interval(
    w: Any,
    intervals: dict[str, int],
    night_hours: int,
) -> tuple[Any, Any, float, float, str]:
    prio = w.priority or "default"
    if getattr(w, "poll_seconds", None):
        int_sec = w.poll_seconds
        int_night_sec = int_sec * 2 if night_hours > 0 else int_sec
        sph_day = 3600.0 / float(int_sec)
        sph_night = 3600.0 / float(int_night_sec)
        int_str = f"{int_sec}s / {int_night_sec}s" if night_hours > 0 else f"{int_sec}s"
        return int_sec / 60.0, int_night_sec / 60.0, sph_day, sph_night, int_str
    int_day = w.poll_minutes if getattr(w, "poll_minutes", None) else intervals.get(prio, 30)
    int_night = int_day * 2 if night_hours > 0 else int_day
    sph_day = 60.0 / int_day
    sph_night = 60.0 / int_night
    int_str = f"{int_day}m / {int_night}m" if night_hours > 0 else f"{int_day}m"
    return int_day, int_night, sph_day, sph_night, int_str


def build_watches_budget_rows(
    watches: list[Any],
    intervals: dict[str, int],
    day_hours: int,
    night_hours: int,
    card_ratio: float,
    fact_7d: dict[str, int],
    history_hours: float,
    days_logged: int,
) -> tuple[list[dict[str, Any]], float, float, float, float, float, float]:
    rows: list[dict[str, Any]] = []
    tot_sph_day = 0.0
    tot_sph_night = 0.0
    tot_searches_day = 0.0
    tot_cards_day = 0.0
    tot_photos_day = 0.0
    tot_daily_reqs = 0.0

    for w in watches:
        row, metrics = _build_watch_budget_row(
            w, intervals, night_hours, day_hours, card_ratio, fact_7d, history_hours, days_logged
        )
        rows.append(row)
        (
            sph_day,
            sph_night,
            searches_day,
            cards_day,
            photos_day,
            total_day,
        ) = metrics
        tot_sph_day += sph_day
        tot_sph_night += sph_night
        tot_searches_day += searches_day
        tot_cards_day += cards_day
        tot_photos_day += photos_day
        tot_daily_reqs += total_day
    return (
        rows,
        tot_sph_day,
        tot_sph_night,
        tot_searches_day,
        tot_cards_day,
        tot_photos_day,
        tot_daily_reqs,
    )


def _build_watch_budget_row(
    w: Any,
    intervals: dict[str, int],
    night_hours: int,
    day_hours: int,
    card_ratio: float,
    fact_7d: dict[str, int],
    history_hours: float,
    days_logged: int,
) -> tuple[dict[str, Any], tuple[float, float, float, float, float, float]]:
    prio = w.priority or "default"
    int_day, int_night, sph_day, sph_night, int_str = calc_watch_interval(w, intervals, night_hours)
    searches_day = (day_hours * sph_day) + (night_hours * sph_night)
    cards_day = searches_day * card_ratio
    photos_day = (cards_day * 2.0) if ("gem" in w.id or getattr(w, "is_gem_feed", False)) else 0.0
    total_day = searches_day + cards_day
    f_cnt = fact_7d.get(w.id, 0)
    fact_str = (
        f"{f_cnt}" if 0 < history_hours < 24.0 else f"{f_cnt} ({f_cnt / float(days_logged):.1f}/д)"
    )
    return (
        {
            "id": w.id,
            "priority": prio,
            "interval_day": int_day,
            "interval_night": int_night,
            "int_str": int_str,
            "sph_day": sph_day,
            "searches_day": searches_day,
            "cards_day": cards_day,
            "photos_day": photos_day,
            "total_day": total_day,
            "fact_str": fact_str,
        },
        (sph_day, sph_night, searches_day, cards_day, photos_day, total_day),
    )


def render_watches_budget_table(
    rows: list[dict[str, Any]],
    history_header: str,
    tot_sph_day: float,
    history_hours: float,
    total_fact_7d: int,
    days_logged: int,
    tot_searches_day: float,
    tot_cards_day: float,
    tot_photos_day: float,
    tot_daily_reqs: float,
) -> tuple[float, str]:
    header = (
        f"{'Товар (ID)':<35} | {'Priority':<8} | {'Interval (D/N)':<14} | "
        f"{'Опросы/сут':<10} | {'Карточки/сут':<12} | {'Фото/сут':<8} | "
        f"{'Итого/сут':<9} | {history_header:<13}"
    )
    sep = "-" * len(header)
    typer.echo(sep)
    typer.echo(header)
    typer.echo(sep)
    for r in rows:
        int_str = r.get("int_str") or f"{r['interval_day']}m / {r['interval_night']}m"
        typer.echo(
            f"{r['id']:<35} | {r['priority']:<8} | {int_str:<14} | "
            f"{r['searches_day']:<10.1f} | {r['cards_day']:<12.1f} | "
            f"{r['photos_day']:<8.1f} | {r['total_day']:<9.1f} | "
            f"{r['fact_str']:<13}"
        )
    typer.echo(sep)
    tot_int_str = f"{tot_sph_day:.1f}/h peak"
    if history_hours > 0 and history_hours < 24.0:
        tot_fact_str = f"{total_fact_7d} (за {history_hours:.0f}ч)"
        daily_fact_total = (total_fact_7d / history_hours) * 24.0
    else:
        daily_fact_total = total_fact_7d / float(days_logged)
        tot_fact_str = f"{total_fact_7d} ({daily_fact_total:.1f}/д)"
    typer.echo(
        f"{'TOTAL (' + str(len(rows)) + ' watches)':<35} | {'':<8} | {tot_int_str:<14} | "
        f"{tot_searches_day:<10.1f} | {tot_cards_day:<12.1f} | {tot_photos_day:<8.1f} | "
        f"{tot_daily_reqs:<9.1f} | {tot_fact_str:<13}"
    )
    return daily_fact_total, sep


def render_worst_case_and_headroom(
    settings: Any,
    watches: list[Any],
    limit: int,
    tot_searches_day: float,
    tot_cards_day: float,
    tot_photos_day: float,
    tot_daily_reqs: float,
    sep: str,
    history_hours: float,
    total_fact_7d: int,
    days_logged: int,
    daily_fact_total: float,
) -> tuple[float, float, float]:
    worst_retries = 15.0
    shadow_cfg = getattr(settings, "shadow", {}) or {}
    max_shadow_cards_cfg = shadow_cfg.get("max_cards_per_day", 40)
    if isinstance(max_shadow_cards_cfg, dict):
        model_cap = int(max_shadow_cards_cfg.get("model_watches", 80))
        gem_cap = int(max_shadow_cards_cfg.get("gem", 15))
        max_shadow_total = model_cap + gem_cap
    else:
        max_shadow_total = int(max_shadow_cards_cfg)

    is_shadow = settings.notify_mode == "shadow" or any(
        getattr(w, "notify_mode", None) == "shadow" for w in watches
    )
    if is_shadow:
        tot_cards_day = min(tot_cards_day, float(max_shadow_total))
        tot_daily_reqs = tot_searches_day + tot_cards_day

    worst_case_reqs = tot_daily_reqs + worst_retries
    typer.echo(
        f"{'WORST CASE (+15 retries)':<35} | {'':<8} | {'':<14} | "
        f"{tot_searches_day + worst_retries:<10.1f} | {tot_cards_day:<12.1f} | "
        f"{tot_photos_day:<8.1f} | {worst_case_reqs:<9.1f} | {'':<13}"
    )
    typer.echo(sep)

    free_expected = max(0.0, limit - tot_daily_reqs)
    free_worst = max(0.0, limit - worst_case_reqs)
    if history_hours > 0 and history_hours < 24.0:
        typer.echo(
            f"Свободный остаток из {limit}: {free_worst:.1f} (наихудший), "
            f"{free_expected:.1f} (ожидаемый), "
            f"установившийся свободный резерв: 50–115/сут "
            f"(факт {history_hours:.0f}ч: {total_fact_7d} запр.)"
        )
    else:
        free_by_fact = max(0.0, limit - daily_fact_total)
        typer.echo(
            f"Свободный остаток из {limit}: {free_worst:.1f} (наихудший), "
            f"{free_expected:.1f} (ожидаемый), "
            f"{free_by_fact:.1f} (по факту {days_logged}д: "
            f"~{daily_fact_total:.1f}/сут, свободно ~50–115/сут)"
        )

    render_coverage_headroom(limit, tot_daily_reqs)
    return tot_cards_day, tot_daily_reqs, worst_case_reqs


def render_coverage_headroom(limit: int, tot_daily_reqs: float) -> None:
    trip_thresh = int(limit * 0.90)
    headroom = trip_thresh - tot_daily_reqs
    typer.echo(
        f"Запас покрытия: {headroom:.1f} запросов/сут "
        f"(порог отключения 90%: {trip_thresh}, плановый расход: "
        f"{tot_daily_reqs:.1f})"
    )
    if headroom < 100:
        typer.secho(
            f"⚠️ ВНИМАНИЕ: Запас покрытия < 100 запросов/сут ({headroom:.1f})! "
            f"Фоновое покрытие (backfill/sweep) может упираться в мягкую паузу "
            f"при пиковой активности.",
            fg=typer.colors.YELLOW,
            bold=True,
        )
        typer.echo("   Рычаги освобождения запаса покрытия:")
        typer.echo(
            "     • Ступень: переключить на Ступень 2 "
            "(/cap 1200 увеличит порог 90% до 1080, дав +240 запаса)"
        )
        typer.echo(
            "     • Интервал gem: увеличить интервалы gem-лент до 120м (освободит +42 запроса/сут)"
        )
        typer.echo(
            "     • Приоритет: изменить интервал normal с 20м на 30м (освободит +42 запроса/сут)"
        )
