from hunter.application.metrics import LowCardinalityMetricsCollector


def test_traffic_summary_distinguishes_unknown_and_measured_samples():
    metrics = LowCardinalityMetricsCollector()
    for size in (100, None, 300):
        metrics.record_poll(
            slot_id="slot",
            status_code=200,
            error=False,
            headers={},
            source_visibility="visible",
            publication_lag_sec=None,
            duration_sec=0.1,
            wire_body_bytes=size,
        )
    summary = metrics.response_body_summary()
    assert summary == {
        "scope": "recent_in_memory_samples",
        "samples": 3,
        "measured_samples": 2,
        "body_bytes": 400,
        "mean_body_bytes": 200.0,
    }


def test_empty_summary_has_unknown_mean():
    summary = LowCardinalityMetricsCollector().response_body_summary()
    assert summary["measured_samples"] == 0
    assert summary["mean_body_bytes"] is None
