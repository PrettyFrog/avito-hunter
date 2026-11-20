import gzip

import httpx
import pytest

from hunter.application.metrics import LowCardinalityMetricsCollector
from hunter.application.public_poll_runtime import QueryAssignment, _record_poll_metrics
from hunter.transport.http_fetch import HttpSlotFetcher


@pytest.mark.asyncio
async def test_poll_reports_compressed_body_bytes_not_decoded_length():
    body = b"repeated-content-" * 1000
    wire = gzip.compress(body)

    class WireStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield wire

    fetcher = HttpSlotFetcher(
        "slot",
        endpoint="http://proxy:8080",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, stream=WireStream(), headers={"content-encoding": "gzip"}
            )
        ),
    )
    try:
        response = await fetcher.fetch("https://www.avito.ru/search")
        assert response.text == body.decode()
        assert response.wire_body_bytes == len(wire)
        metrics = LowCardinalityMetricsCollector()
        assignment = QueryAssignment(
            "slot", "http://proxy:8080", "query", "https://example.invalid", 1, 0
        )
        _record_poll_metrics(
            metrics, assignment, response, error=False, source_visibility="visible"
        )
        assert metrics.poll_samples[-1].wire_body_bytes == len(wire)
    finally:
        await fetcher.close()
