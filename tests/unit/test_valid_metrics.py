"""Tests for physical HTTP accounting, valid catalog updates, gaps, and downtime (H-04)."""
from hunter.application.metrics import LowCardinalityMetricsCollector
from hunter.application.poll_attempts import assemble_poll_batch
from hunter.application.valid_metrics import ValidMetricsTracker
from hunter.domain.observations import ParseOutcome


class DummyResponse:
    def __init__(self, status_code=200, text="", headers=None, elapsed_sec=0.1):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}
        self.elapsed_sec = elapsed_sec
        self.wire_body_bytes = len(text.encode())


class DummyAssignment:
    def __init__(self, slot_id="slot-1", query_id="q-shoes", canonical_url="https://avito.ru/shoes"):
        self.slot_id = slot_id
        self.query_id = query_id
        self.canonical_url = canonical_url
        self.generation = 1
        self.endpoint = "direct"


def test_valid_update_accounting_and_attempts_ratio():
    tracker = ValidMetricsTracker(clock=lambda: 100.0)
    tracker.record_physical_http("poll")
    tracker.record_physical_http("poll")
    tracker.record_physical_http("warmup")
    tracker.record_physical_http("retry")
    tracker.record_physical_http("redirect")
    tracker.record_physical_http("challenge")

    assert tracker.total_physical_http() == 6
    assert tracker.physical_http_counts["poll"] == 2
    assert tracker.physical_http_counts["warmup"] == 1
    assert tracker.physical_http_counts["challenge"] == 1

    # Record 2 valid updates
    tracker.record_valid_update("q-1", items_count=20, now=100.0)
    tracker.record_valid_update("q-1", items_count=25, now=400.0)
    assert tracker.total_valid_updates == 2
    assert tracker.attempts_per_valid_update() == 3.0  # 6 physical / 2 valid


def test_invalid_responses_do_not_become_valid_updates():
    metrics = LowCardinalityMetricsCollector()
    assignment = DummyAssignment(query_id="q-electronics")

    # 1. HTTP 429 rate limit response
    resp_429 = DummyResponse(status_code=429, text="Too Many Requests")
    batch_429 = assemble_poll_batch(assignment, resp_429, ParseOutcome.RATE_LIMIT_429, None, metrics)
    assert batch_429.outcome == ParseOutcome.RATE_LIMIT_429
    assert metrics.valid_tracker.total_valid_updates == 0

    # 2. HTTP 200 with captcha/challenge shell
    resp_captcha = DummyResponse(status_code=200, text="<html><body>captcha</body></html>")
    batch_captcha = assemble_poll_batch(assignment, resp_captcha, ParseOutcome.CAPTCHA, None, metrics)
    assert batch_captcha.outcome == ParseOutcome.CAPTCHA
    assert metrics.valid_tracker.total_valid_updates == 0

    # 3. HTTP 403 blocked
    resp_403 = DummyResponse(status_code=403, text="Forbidden")
    batch_403 = assemble_poll_batch(assignment, resp_403, ParseOutcome.BLOCKED_403, None, metrics)
    assert batch_403.outcome == ParseOutcome.BLOCKED_403
    assert metrics.valid_tracker.total_valid_updates == 0

    # 4. Valid catalog response with items
    valid_html = '<html><body><div data-marker="item" data-item-id="12345"><h4>Phone</h4><span>10 000 ₽</span></div></body></html>'
    resp_ok = DummyResponse(status_code=200, text=valid_html)
    batch_ok = assemble_poll_batch(assignment, resp_ok, ParseOutcome.OK, None, metrics)
    assert batch_ok.outcome == ParseOutcome.OK
    assert metrics.valid_tracker.total_valid_updates == 1


def test_long_outage_does_not_disappear_from_p95_and_downtime():
    now = [1000.0]
    tracker = ValidMetricsTracker(clock=lambda: now[0])

    # 3 valid updates spaced by 300s (normal interval)
    tracker.record_valid_update("q-cars", items_count=10, now=now[0])
    now[0] += 300.0
    tracker.record_valid_update("q-cars", items_count=10, now=now[0])
    now[0] += 300.0
    tracker.record_valid_update("q-cars", items_count=10, now=now[0])

    stats_normal = tracker.query_gap_summary("q-cars", now=now[0], expected_interval_sec=300.0)
    assert stats_normal.mean_gap_sec == 300.0
    assert stats_normal.p50_gap_sec == 300.0
    assert stats_normal.p95_gap_sec == 300.0
    assert stats_normal.downtime_sec == 0.0

    # Severe outage: 5000 seconds pass without any valid update
    now[0] += 5000.0
    stats_outage = tracker.query_gap_summary("q-cars", now=now[0], expected_interval_sec=300.0)

    # Incomplete tail window (5000s) must be included in p95 and downtime
    assert stats_outage.tail_window_sec == 5000.0
    assert stats_outage.p95_gap_sec >= 4500.0
    # Downtime must reflect the excess outage beyond expected interval (5000 - 300 = 4700)
    assert stats_outage.downtime_sec == 4700.0


def test_scheduled_polls_quarantine_and_html_rediscovery():
    tracker = ValidMetricsTracker(clock=lambda: 100.0)
    tracker.record_scheduled_poll(executed=True)
    tracker.record_scheduled_poll(executed=True)
    tracker.record_scheduled_poll(executed=False)  # Deferred / skipped
    assert tracker.scheduled_polls_done == 2
    assert tracker.scheduled_polls_skipped == 1

    tracker.record_quarantine_duration(180.0)  # 3 minutes
    tracker.record_html_rediscovery()
    tracker.record_html_rediscovery()

    summary = tracker.summary()
    assert summary["scheduled_polls_done"] == 2
    assert summary["scheduled_polls_skipped"] == 1
    assert summary["quarantine_minutes"] == 3.0
    assert summary["html_rediscovery_count"] == 2

    # Check PII safety: summary must not contain raw URLs or credentials
    summary_str = str(summary)
    for forbidden in ("http://", "https://", "cookie", "token", "auth", "secret"):
        assert forbidden not in summary_str.lower()


def test_observed_listings_photo_propagation():
    from hunter.application.poll_attempts import _observed_listings
    from hunter.core.models import SearchHit

    hit1 = SearchHit(
        avito_id="123", url="https://avito.ru/123", title="Item with photo",
        price=1000, price_state="ok", city="Moscow",
        extra={"photos": ["https://img.avito.st/image/1/test_636.jpg", "https://img.avito.st/image/1/test_2.jpg"]}
    )
    hit2 = SearchHit(
        avito_id="456", url="https://avito.ru/456", title="Item without photo",
        price=2000, price_state="ok", city="Moscow",
        extra={}
    )
    listings = _observed_listings([hit1, hit2])
    assert len(listings) == 2
    assert listings[0].image_url == "https://img.avito.st/image/1/test_636.jpg"
    assert listings[1].image_url is None

