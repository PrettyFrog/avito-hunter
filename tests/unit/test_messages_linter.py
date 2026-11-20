from __future__ import annotations

import re

from hunter.notify import messages_ru

FORBIDDEN_JARGON = [
    r"\byaml\b",
    r"\bjob\b",
    r"\bjobs\b",
    r"\bpoison\b",
    r"\bcap\b",
    r"\bpmax\b",
    r"\bvlm\b",
    r"\bapi\b",
    r"\bтокен\w*",
    r"\bлимит[её]р\w*",
    r"\bshadow\b",
    r"\bcircuit\b",
    r"\br2\b",
    r"\bround1\b",
    r"\bround2\b",
    r"\bstage\b",
]


def test_messages_ru_contains_no_forbidden_jargon():
    """Verify that all strings defined in messages_ru are free of forbidden technical jargon."""
    checked_count = 0
    for attr_name in dir(messages_ru):
        if attr_name.startswith("_"):
            continue
        val = getattr(messages_ru, attr_name)
        if isinstance(val, str):
            checked_count += 1
            lower_val = val.lower()
            for pattern in FORBIDDEN_JARGON:
                match = re.search(pattern, lower_val, re.IGNORECASE)
                assert match is None, (
                    f"Forbidden jargon '{match.group(0)}' (pattern: {pattern}) "
                    f"found in messages_ru.{attr_name}: '{val}'"
                )

    assert checked_count >= 8, f"Expected at least 8 message templates checked, got {checked_count}"


def test_messages_ru_sanitize_html_escape():
    """Verify HTML escaping for untrusted user input."""
    raw = "<script>alert('xss')</script> & \"foo\""
    sanitized = messages_ru.sanitize(raw)
    assert "&lt;script&gt;" in sanitized
    assert "&amp;" in sanitized
    assert "&quot;foo&quot;" in sanitized
