"""Regression test for F-077: Gzip stream truncation in scrub_xhr_blobs.

Demonstrates that scrub_xhr_blobs outputs a valid gzip stream that can always
be decompressed without EOFError, and whose uncompressed content is valid JSON,
even when input size exceeds 200KB.
"""

from __future__ import annotations

import gzip
import json
import os

from hunter.core.rawstore import scrub_xhr_blobs


def test_scrub_xhr_blobs_decompression_integrity() -> None:
    # Generate high-entropy sample data to exceed 200KB compressed
    blobs = [{"id": i, "token": os.urandom(128).hex()} for i in range(2500)]
    compressed = scrub_xhr_blobs(blobs)

    assert len(compressed) <= 200 * 1024, f"Output exceeds 200KB: {len(compressed)} bytes"

    # Must decompress cleanly without EOFError or zlib.error
    decompressed = gzip.decompress(compressed)
    assert len(decompressed) > 0

    # Must be valid parseable JSON
    data = json.loads(decompressed.decode("utf-8"))
    assert isinstance(data, list)


def test_scrub_xhr_blobs_empty_and_normal() -> None:
    # Empty list
    res_empty = scrub_xhr_blobs([])
    assert json.loads(gzip.decompress(res_empty).decode("utf-8")) == []

    # Normal list
    normal = [{"sellerName": "Иван", "phone": "+7 999 123-45-67"}]
    res_normal = scrub_xhr_blobs(normal)
    data = json.loads(gzip.decompress(res_normal).decode("utf-8"))
    assert data[0]["sellerName"] == "[name]"
    assert data[0]["phone"] == "[phone]"
