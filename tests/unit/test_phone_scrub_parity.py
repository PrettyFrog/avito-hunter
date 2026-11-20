"""Preserve phone redaction boundaries while optimizing the scanner."""

import gzip
import random
import re
from pathlib import Path

import pytest

from hunter.core.rawstore import PHONE_RE


def original_pattern():
    return re.compile(
        r"(?<![\d\.])(?:\+7|8)[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}"
        r"[\s\-]?\d{2}[\s\-]?\d{2}(?!\d)"
    )


def test_phone_redaction_preserves_unicode_and_adjacent_boundaries():
    previous = original_pattern()
    rng = random.Random(20261102)
    for _ in range(20000):
        prefix = "".join(rng.choices("a.078+\n ١²", k=rng.randrange(6)))
        suffix = "".join(rng.choices("a.078+\n ١²", k=rng.randrange(6)))
        number = (rng.choice(["+7", "8"]) + rng.choice(["", " ", "-", "\n"])
                  + rng.choice(["123", "(123)", "١٢٣"]) + rng.choice(["", " ", "-"])
                  + "456" + rng.choice(["", " ", "-"]) + "78"
                  + rng.choice(["", " ", "-"]) + "90")
        text = prefix + number + suffix
        assert PHONE_RE.sub("[phone]", text) == previous.sub("[phone]", text)


@pytest.mark.parametrize("name", [
    "real_bose_ultra_search.html.gz", "search_bose_ultra.html.gz",
    "search_bw_px8.html.gz", "search_sony_xm6.html.gz",
])
def test_recorded_html_redaction_is_identical(name):
    fixture = Path(__file__).parents[1] / "fixtures" / "html" / name
    text = gzip.decompress(fixture.read_bytes()).decode()
    assert PHONE_RE.sub("[phone]", text) == original_pattern().sub("[phone]", text)
