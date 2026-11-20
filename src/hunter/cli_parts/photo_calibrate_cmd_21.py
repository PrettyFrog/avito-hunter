from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
import typer

from hunter.cli_parts.app_01 import app
from hunter.core.db import get_db


def _render_calibration_header(rep: Any, product_id: str) -> None:
    typer.echo("=" * 76)
    typer.echo(f"📸 КАЛИБРОВКА ФОТО-ПРОВЕРКИ ДЛЯ ТОВАРА: {product_id} (СЛЕПОЙ РЕЖИМ)")
    typer.echo("=" * 76)
    typer.echo(f"Всего протестировано: {rep.total_samples}")
    typer.echo(
        f"Целевых (положительных): {rep.total_positives} | "
        f"Чужих моделей (отрицательных): {rep.total_negatives}"
    )


def _render_combined_metrics(rep: Any) -> None:
    typer.echo("-" * 76)
    typer.echo("ИТОГОВАЯ МАТРИЦА РЕШЕНИЙ (COMBINED):")
    typer.echo(
        f"  True Positives:  {rep.true_positives:<4} | False Positives: {rep.false_positives}"
    )
    typer.echo(
        f"  True Negatives:  {rep.true_negatives:<4} | False Negatives: {rep.false_negatives}"
    )
    typer.echo(f"  Неоднозначных (unclear): {rep.unclear_samples}")
    correct = rep.true_positives + rep.true_negatives
    wrong = rep.false_positives + rep.false_negatives
    typer.echo(
        f"  Распределение: правильно: {correct} | неверно: {wrong} | unclear: {rep.unclear_samples}"
    )
    typer.echo(f"  Точность распознавания чужих (Precision 'no'): {rep.precision_no:.1%}")
    typer.echo(f"  Полнота отсева чужих (Recall 'no'):           {rep.recall_neg:.1%}")
    typer.echo(f"  Полнота целевых (Recall target):               {rep.recall_target:.1%}")
    typer.echo(f"  Общая точность (Accuracy):                     {rep.accuracy:.1%}")


def _render_method_metrics(label: str, metrics: Any) -> None:
    correct = metrics.true_positives + metrics.true_negatives
    wrong = metrics.false_positives + metrics.false_negatives
    typer.echo(f"  Метод {label}:")
    typer.echo(f"    Правильно: {correct} | Неверно: {wrong} | Unclear: {metrics.unclear_samples}")
    typer.echo(
        f"    TP: {metrics.true_positives}, TN: {metrics.true_negatives}, "
        f"FP: {metrics.false_positives}, FN: {metrics.false_negatives}"
    )
    typer.echo(
        f"    Precision(no): {metrics.precision_no:.1%} | "
        f"Recall(target): {metrics.recall_target:.1%} | "
        f"Accuracy: {metrics.accuracy:.1%}"
    )


def _render_provider_stats(provider_stats: Any) -> None:
    if not provider_stats:
        return
    typer.echo("СТАТИСТИКА ПРОВАЙДЕРОВ VLM (llm_calls):")
    for provider_name, stats in provider_stats.items():
        typer.echo(
            f"  - {provider_name}: {stats.calls} вызовов, "
            f"среднее время: {stats.avg_latency_sec:.2f}с, ошибок: {stats.errors}"
        )
        typer.echo(
            f"    Токены: prompt ≈ {stats.avg_tokens_prompt:.0f}, "
            f"completion ≈ {stats.avg_tokens_completion:.0f}, "
            f"всего ≈ {stats.avg_tokens_total:.0f}"
        )
        typer.echo("-" * 76)


def _render_qualification(qualified_hard_skip: bool) -> None:
    qual_mark = (
        "✅ КВАЛИФИЦИРОВАН ДЛЯ HARD SKIP (≥20 негативов и Precision(no) ≥ 95%)"
        if qualified_hard_skip
        else "❌ НЕ КВАЛИФИЦИРОВАН ДЛЯ HARD SKIP (требуется ≥20 негативов и Precision(no) ≥ 95%)"
    )
    typer.echo(f"Квалификация Hard Skip (B3): {qual_mark}")


def _render_calibration_report_metrics(rep: Any, product_id: str) -> None:
    _render_calibration_header(rep, product_id)
    _render_combined_metrics(rep)
    typer.echo("-" * 76)
    typer.echo("РАЗДЕЛЬНЫЕ МЕТРИКИ ПО МЕТОДАМ (D vs C):")
    _render_method_metrics("D (Describe)", rep.metrics_d)
    _render_method_metrics("C (Compare)", rep.metrics_c)
    typer.echo("-" * 76)
    _render_provider_stats(rep.provider_stats)
    _render_qualification(rep.qualified_hard_skip)
    typer.echo("=" * 76)


@app.command(name="photo-calibrate")
def photo_calibrate_cmd(
    product_id: str = typer.Argument(..., help="Product ID (e.g. 'sony_xm6')"),
    limit: int = typer.Option(50, "--limit", "-n", help="Max samples to calibrate"),
    labels_file: Path | None = typer.Option(
        None, "--labels-file", help="Path to JSON file with ground truth labels"
    ),
) -> None:
    """Evaluate historical images against labels and qualify hard skip (B1, B3)."""
    typer.echo("ℹ️ Фото-калибровка устарела в LotRadar v2.")


@app.command(name="photo-report")
def photo_report_cmd(
    product_id: str = typer.Argument(..., help="Product ID (e.g. 'sony_xm6')"),
) -> None:
    """Display photo verification report for shadow mode lots (B2)."""
    typer.echo("ℹ️ Фото-отчет устарел в LotRadar v2.")


jobs_app = typer.Typer(no_args_is_help=True, help="Manage persistent job queue")
app.add_typer(jobs_app, name="jobs")


@jobs_app.command(name="list")
def jobs_list_cmd(
    poison: bool = typer.Option(False, "--poison", help="List only poison jobs"),
    limit: int = typer.Option(50, "--limit", "-n", help="Max jobs to display"),
) -> None:
    """List jobs in persistent SQLite queue."""

    async def _list() -> None:
        db = get_db()
        await db.connect()
        status_filter = "poison" if poison else None
        jobs = await db.list_jobs(status=status_filter, limit=limit)
        await db.close()

        if not jobs:
            typer.echo(
                "В очереди нет подходящих задач." if not poison else "В очереди нет poison-задач."
            )
            return

        title_str = "POISON ЗАДАЧИ В ОЧЕРЕДИ" if poison else "ЗАДАЧИ В ОЧЕРЕДИ JOBS"
        typer.echo("=" * 80)
        typer.echo(f"📋 {title_str} (всего: {len(jobs)})")
        typer.echo("=" * 80)
        hdr = (
            f"{'ID':<6} | {'Kind':<14} | {'Product':<20} | {'Status':<8} | "
            f"{'Att':<4} | {'Dedupe Key':<12} | {'Last Error'}"
        )
        typer.echo(hdr)
        typer.echo("-" * 80)
        for j in jobs:
            err = (j.get("last_error") or "")[:40]
            typer.echo(
                f"{j['id']:<6} | {j['kind']:<14} | "
                f"{(j.get('product_id') or '-'):<20} | {j['status']:<8} | "
                f"{j['attempts']:<4} | {(j.get('dedupe_key') or '-'):<12} | {err}"
            )
        typer.echo("=" * 80)

    asyncio.run(_list())


@jobs_app.command(name="retry")
def jobs_retry_cmd(
    job_id: int | None = typer.Argument(None, help="Job ID to retry"),
    all_poison: bool = typer.Option(False, "--all-poison", help="Retry all poison jobs"),
) -> None:
    """Retry failed or poison jobs by setting status back to 'queued' and attempts=0."""

    async def _retry() -> None:
        db = get_db()
        conn = await db.connect()
        if all_poison:
            cur = await conn.execute(
                "UPDATE jobs SET status = 'queued', attempts = 0, last_error = NULL, "
                "next_run_at = NULL WHERE status = 'poison'"
            )
            await conn.commit()
            typer.echo(f"Перезапущено poison-задач: {cur.rowcount}")
        elif job_id is not None:
            cur = await conn.execute(
                "UPDATE jobs SET status = 'queued', attempts = 0, last_error = NULL, "
                "next_run_at = NULL WHERE id = ?",
                (job_id,),
            )
            await conn.commit()
            if cur.rowcount > 0:
                typer.echo(f"Задача #{job_id} возвращена в очередь.")
            else:
                typer.echo(f"Задача #{job_id} не найдена.")
        else:
            typer.echo("Укажите ID задачи или флаг --all-poison.")
        await db.close()

    asyncio.run(_retry())


@app.command(name="audit-skips")
def audit_skips_cmd(
    days: int = typer.Option(7, "--days", "-d", help="Number of past days to audit"),
    n: int = typer.Option(40, "--n", "-n", help="Max sample size of skipped lots"),
    send: bool = typer.Option(False, "--send", help="Send stratified sample to owner in Telegram"),
) -> None:
    """Audit skipped gems by reason and optionally send an owner review sample (C1)."""

    async def _audit() -> None:
        from hunter.audit.skips import audit_skips
        from hunter.notify.telegram import TelegramChannel

        db = get_db()
        await db.connect()
        notifier = TelegramChannel() if send else None
        res = await audit_skips(db, days=days, n=n, send_telegram=send, notifier=notifier)
        await db.close()

        typer.echo("=" * 80)
        typer.echo(f"🔍 АУДИТ ОТСЕВА В GEM-ЛЕНТАХ ЗА ПОСЛЕДНИЕ {res['days']} ДНЕЙ (PR-3.10 C1)")
        typer.echo("=" * 80)
        typer.echo("РАЗБИВКА ОТСЕЯННЫХ ЛОТОВ ПО ПРИЧИНАМ:")
        for wid, reasons in res["breakdown"].items():
            typer.echo(f"\n📦 Лента `{wid}` (потолок цены: {res['ceilings'].get(wid, '-')} ₽):")
            for r_name, cnt in sorted(reasons.items(), key=lambda x: -x[1]):
                typer.echo(f"  • {r_name:<20}: {cnt}")
        typer.echo("\n" + "-" * 80)
        typer.echo(
            f"СТРАТИФИЦИРОВАННАЯ ВЫБОРКА ОТСЕЯННЫХ ЛОТОВ (≤ ПОТОЛКА, ВСЕГО {len(res['sample'])}):"
        )
        typer.echo("-" * 80)
        for i, s in enumerate(res["sample"], 1):
            typer.echo(f"[{i:02d}] {s['watchlist_id']} | {s['price']:,} ₽ | Причина: {s['reason']}")
            typer.echo(f"     Заголовок: {s['title']}")
            typer.echo(f"     В описании: {s['desc_target']} | URL: {s['url']}")
        typer.echo("=" * 80)
        if send:
            typer.echo(
                f"✅ Отправлено владельцу в Telegram: {res['sent_count']} "
                "сообщений с кнопками аудита."
            )

    asyncio.run(_audit())


@app.command(name="explain")
def explain_cmd(
    target: str = typer.Argument(..., help="Avito listing ID or URL to explain"),
) -> None:
    """Detailed audit for a listing (deprecated in v2)."""
    typer.echo("ℹ️ Команда explain устарела в LotRadar v2.")


@app.command(name="reeval")
def reeval_cmd(
    avito_id: str = typer.Argument(None, help="Specific Avito ID to re-evaluate"),
    stubs: bool = typer.Option(
        False, "--stubs", help="Re-evaluate all listings rejected with stub photos"
    ),
) -> None:
    """Rerun evaluation (deprecated in v2)."""
    typer.echo("ℹ️ Команда reeval устарела в LotRadar v2.")
