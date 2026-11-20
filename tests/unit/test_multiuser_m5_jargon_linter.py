"""Check user-facing documentation for technical jargon and required disclaimers.

Verifies that the user guide and message text avoid internal terms, and that all
four mandatory disclaimers appear in both user-facing surfaces.
"""

from __future__ import annotations

import re
from pathlib import Path

from hunter.notify import messages_ru

FORBIDDEN_JARGON = [
    r"\byaml\b",
    r"\bcron\b",
    r"\bdaemon\b",
    r"\bsqlite\b",
    r"\bpoller\b",
    r"\bapi\b",
    r"\bwebhook\b",
    r"\bpid\b",
    r"\bjson\b",
    r"\bdeploy\b",
    r"\bplaywright\b",
    r"\bscraping\b",
    r"\bscraper\b",
    r"\bcookie\b",
]

MANDATORY_DISCLAIMERS = [
    "Бот частный и не связан с Авито.",
    "Владелец бота видит список ваших товаров.",
    "Принимаются только поисковые ссылки Авито.",
    "Уведомления могут приходить с задержкой.",
]


def test_user_guide_disclaimers_present() -> None:
    guide_path = Path("docs/USER_GUIDE.md")
    assert guide_path.exists(), "docs/USER_GUIDE.md must exist"
    content = guide_path.read_text(encoding="utf-8")

    for disclaimer in MANDATORY_DISCLAIMERS:
        assert disclaimer in content, f"Missing disclaimer in USER_GUIDE.md: {disclaimer}"


def test_messages_ru_disclaimers_present() -> None:
    assert hasattr(messages_ru, "DISCLAIMERS_BLOCK")
    assert hasattr(messages_ru, "GUIDE_TEXT")

    for disclaimer in MANDATORY_DISCLAIMERS:
        assert disclaimer in messages_ru.DISCLAIMERS_BLOCK, (
            f"Missing disclaimer in DISCLAIMERS_BLOCK: {disclaimer}"
        )
        assert disclaimer in messages_ru.GUIDE_TEXT, (
            f"Missing disclaimer in GUIDE_TEXT: {disclaimer}"
        )


def test_user_guide_no_forbidden_jargon() -> None:
    guide_path = Path("docs/USER_GUIDE.md")
    content = guide_path.read_text(encoding="utf-8").lower()

    # Mask URLs like https://... so "http" inside URLs doesn't trigger
    sanitized = re.sub(r"https?://\S+", "", content)

    for pattern in FORBIDDEN_JARGON:
        match = re.search(pattern, sanitized, re.IGNORECASE)
        assert match is None, f"Forbidden jargon '{match.group(0)}' found in docs/USER_GUIDE.md"


def test_guide_text_no_forbidden_jargon() -> None:
    guide_text = messages_ru.GUIDE_TEXT.lower()
    sanitized = re.sub(r"https?://\S+", "", guide_text)

    for pattern in FORBIDDEN_JARGON:
        match = re.search(pattern, sanitized, re.IGNORECASE)
        assert match is None, f"Forbidden jargon '{match.group(0)}' found in GUIDE_TEXT"


def test_help_summary_no_forbidden_jargon() -> None:
    help_text = messages_ru.HELP_COMMANDS_SUMMARY.lower()
    sanitized = re.sub(r"https?://\S+", "", help_text)

    for pattern in FORBIDDEN_JARGON:
        match = re.search(pattern, sanitized, re.IGNORECASE)
        assert match is None, f"Forbidden jargon '{match.group(0)}' found in HELP_COMMANDS_SUMMARY"
