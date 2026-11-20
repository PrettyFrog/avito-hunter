from __future__ import annotations

import gzip
import json
import re
from pathlib import Path

PHONE_RE = re.compile(
    # Consume the literal prefix first so regex can skip non-candidate text.
    # Each fixed-width lookbehind checks the same preceding character as before.
    r"(?:\+7(?<![\d.]\+7)|8(?<![\d.]8))[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}"
    r"[\s\-]?\d{2}[\s\-]?\d{2}(?!\d)"
)
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b")
TG_RE = re.compile(r"(?:t\.me|telegram\.me)/[A-Za-z0-9_]+", re.I)
WA_RE = re.compile(r"(?:wa\.me|whatsapp)[^\s<]{0,40}", re.I)
TG_BOT_TOKEN_RE = re.compile(r"(api\.telegram\.org/bot)\d+:[A-Za-z0-9_\-]+", re.I)
SECRET_KEY_RE = re.compile(
    r"(?i)((?:api[_-]?key|password|secret|token|bearer)\s*[:=]\s*['\"]?)[A-Za-z0-9_\-\.]{16,}(['\"]?)"
)


def scrub_text(text: str) -> str:
    text = PHONE_RE.sub("[phone]", text)
    text = EMAIL_RE.sub("[email]", text)
    text = TG_RE.sub("[messenger]", text)
    text = WA_RE.sub("[messenger]", text)
    text = TG_BOT_TOKEN_RE.sub(r"\1[REDACTED]", text)
    text = SECRET_KEY_RE.sub(r"\1[REDACTED]\2", text)
    return text


NAME_KEY_RE = re.compile(
    r'("(?:sellerName|name|person|contactName|userName)"\s*:\s*)"([^"]*)"',
    re.IGNORECASE,
)


def scrub_xhr_blobs(blobs: list[object], max_bytes: int = 200 * 1024) -> bytes:
    safe_blobs = list(blobs)
    while safe_blobs:
        dumped = json.dumps(safe_blobs, ensure_ascii=False)
        scrubbed = scrub_text(dumped)
        scrubbed = NAME_KEY_RE.sub(r'\1"[name]"', scrubbed)
        compressed = gzip.compress(scrubbed.encode("utf-8"))
        if len(compressed) <= max_bytes:
            return compressed
        if len(safe_blobs) == 1:
            note = [{"_truncated": True, "_warning": "blob exceeded maximum sample size"}]
            return gzip.compress(json.dumps(note).encode("utf-8"))
        safe_blobs = safe_blobs[: len(safe_blobs) // 2]
    return gzip.compress(b"[]")


def write_raw(directory: Path, avito_id: str, kind: str, payload: str | dict) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{avito_id}_{kind}.html.gz"
    if isinstance(payload, dict):
        body = json.dumps(payload, ensure_ascii=False)
        path = directory / f"{avito_id}_{kind}.json.gz"
    else:
        body = payload
    body = scrub_text(body)
    path.write_bytes(gzip.compress(body.encode("utf-8")))
    return path
