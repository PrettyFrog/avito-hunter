from types import SimpleNamespace

from hunter.cli_parts.photo_calibrate_cmd_21 import _render_calibration_report_metrics


def _method_metrics(
    tp: int,
    fp: int,
    tn: int,
    fn: int,
    unclear: int,
    precision: float,
    target_recall: float,
    accuracy: float,
) -> SimpleNamespace:
    return SimpleNamespace(
        true_positives=tp,
        false_positives=fp,
        true_negatives=tn,
        false_negatives=fn,
        unclear_samples=unclear,
        precision_no=precision,
        recall_target=target_recall,
        accuracy=accuracy,
    )


def _report(*, qualified: bool = True, providers: bool = True) -> SimpleNamespace:
    stats = SimpleNamespace(
        calls=2,
        avg_latency_sec=0.25,
        errors=1,
        avg_tokens_prompt=10,
        avg_tokens_completion=3,
        avg_tokens_total=13,
    )
    return SimpleNamespace(
        total_samples=5,
        total_positives=2,
        total_negatives=3,
        true_positives=1,
        false_positives=1,
        true_negatives=2,
        false_negatives=0,
        unclear_samples=2,
        precision_no=0.667,
        recall_neg=0.75,
        recall_target=0.5,
        accuracy=0.6,
        metrics_d=_method_metrics(1, 0, 2, 1, 0, 0.8, 0.5, 0.75),
        metrics_c=_method_metrics(0, 1, 2, 1, 1, 0.667, 0.0, 0.5),
        provider_stats={"test-provider": stats} if providers else {},
        qualified_hard_skip=qualified,
    )


def test_calibration_report_matches_full_text_and_section_order(capsys) -> None:
    _render_calibration_report_metrics(_report(), "sample_item")
    expected = """============================================================================
📸 КАЛИБРОВКА ФОТО-ПРОВЕРКИ ДЛЯ ТОВАРА: sample_item (СЛЕПОЙ РЕЖИМ)
============================================================================
Всего протестировано: 5
Целевых (положительных): 2 | Чужих моделей (отрицательных): 3
----------------------------------------------------------------------------
ИТОГОВАЯ МАТРИЦА РЕШЕНИЙ (COMBINED):
  True Positives:  1    | False Positives: 1
  True Negatives:  2    | False Negatives: 0
  Неоднозначных (unclear): 2
  Распределение: правильно: 3 | неверно: 1 | unclear: 2
  Точность распознавания чужих (Precision 'no'): 66.7%
  Полнота отсева чужих (Recall 'no'):           75.0%
  Полнота целевых (Recall target):               50.0%
  Общая точность (Accuracy):                     60.0%
----------------------------------------------------------------------------
РАЗДЕЛЬНЫЕ МЕТРИКИ ПО МЕТОДАМ (D vs C):
  Метод D (Describe):
    Правильно: 3 | Неверно: 1 | Unclear: 0
    TP: 1, TN: 2, FP: 0, FN: 1
    Precision(no): 80.0% | Recall(target): 50.0% | Accuracy: 75.0%
  Метод C (Compare):
    Правильно: 2 | Неверно: 2 | Unclear: 1
    TP: 0, TN: 2, FP: 1, FN: 1
    Precision(no): 66.7% | Recall(target): 0.0% | Accuracy: 50.0%
----------------------------------------------------------------------------
СТАТИСТИКА ПРОВАЙДЕРОВ VLM (llm_calls):
  - test-provider: 2 вызовов, среднее время: 0.25с, ошибок: 1
    Токены: prompt ≈ 10, completion ≈ 3, всего ≈ 13
----------------------------------------------------------------------------
Квалификация Hard Skip (B3): ✅ КВАЛИФИЦИРОВАН ДЛЯ HARD SKIP (≥20 негативов и Precision(no) ≥ 95%)
============================================================================
"""
    assert capsys.readouterr().out == expected


def test_calibration_report_handles_empty_providers_and_unqualified_result(capsys) -> None:
    _render_calibration_report_metrics(_report(qualified=False, providers=False), "sample_item")
    output = capsys.readouterr().out
    assert "СТАТИСТИКА ПРОВАЙДЕРОВ VLM" not in output
    assert "Квалификация Hard Skip (B3): ❌ НЕ КВАЛИФИЦИРОВАН" in output
