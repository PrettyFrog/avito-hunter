"""Qrator WAF offline XTEA codec and challenge handler (S5 / T-480)."""

from __future__ import annotations

import json
import os
import re
import secrets
import struct
import time
from typing import Any

DELTA = 0x9E3779B9
ROUNDS = 32
DEFAULT_KEY = (1901231474, 1081891380, 1433695566, 978402641)

DEFAULT_BASE_F_TEMPLATE = (
    "1920;1080;24;24;240;180;240;180;240;180;240;180;240;240;240;240;240;240;"
    "240;180;180;180;180;180;1;1;1;1;1;1;0;1;0;1;1;1;1;1;0;1;1;1;1;0;0;{cpu_count};"
    "3021059336;356363667;1;2;2;2;0;2;1;1;1;1;1;1;0;1;0;0;0;0;0;0;0;0;0;0;0;"
    "1;0;0;0;0;0;0;1;0;0;0;1;0;0;0;0;0;0;1;0;0;0;0;0;0;1;0;0;0;0;5;2485918234;"
    "2819401823;{canvas_hash};3469306551;1394145034;1320192270;1964592505;0;0;0;"
    "1;1;1;1;1;1;1;1;1;1;2;1;1;1;{timestamp};0;0;0;0;0;0;0;0;0;0;3517267889;3369925510"
)

DEFAULT_BASE_S = '{"monospace":false,"readOnly":false,"noLengthPlugins":false,"installedExtensions":[]}'


def _encrypt_block(block: bytes, key: tuple[int, int, int, int] = DEFAULT_KEY) -> bytes:
    left, right = struct.unpack("<2I", block)
    total = 0
    for _ in range(ROUNDS):
        left = (left + (((((right << 4) & 0xFFFFFFFF) ^ (right >> 5)) + right) ^ ((total + key[total & 3]) & 0xFFFFFFFF))) & 0xFFFFFFFF
        total = (total + DELTA) & 0xFFFFFFFF
        right = (right + (((((left << 4) & 0xFFFFFFFF) ^ (left >> 5)) + left) ^ ((total + key[(total >> 11) & 3]) & 0xFFFFFFFF))) & 0xFFFFFFFF
    return struct.pack("<2I", left, right)


def _decrypt_block(block: bytes, key: tuple[int, int, int, int] = DEFAULT_KEY) -> bytes:
    left, right = struct.unpack("<2I", block)
    total = (DELTA * ROUNDS) & 0xFFFFFFFF
    for _ in range(ROUNDS):
        right = (right - (((((left << 4) & 0xFFFFFFFF) ^ (left >> 5)) + left) ^ ((total + key[(total >> 11) & 3]) & 0xFFFFFFFF))) & 0xFFFFFFFF
        total = (total - DELTA) & 0xFFFFFFFF
        left = (left - (((((right << 4) & 0xFFFFFFFF) ^ (right >> 5)) + right) ^ ((total + key[total & 3]) & 0xFFFFFFFF))) & 0xFFFFFFFF
    return struct.pack("<2I", left, right)


def encrypt_bytes(raw: bytes, key: tuple[int, int, int, int] = DEFAULT_KEY) -> str:
    if not raw:
        return ""
    padded = raw + b"\0" * (-len(raw) % 8)
    return "".join(_encrypt_block(padded[i : i + 8], key).hex() for i in range(0, len(padded), 8))


def decrypt_bytes(ciphertext_hex: str, key: tuple[int, int, int, int] = DEFAULT_KEY) -> bytes:
    if not ciphertext_hex:
        return b""
    raw = bytes.fromhex(ciphertext_hex)
    if len(raw) % 8 != 0:
        raise ValueError("Ciphertext length must be a multiple of 8 bytes")
    decrypted = b"".join(_decrypt_block(raw[i : i + 8], key) for i in range(0, len(raw), 8))
    return decrypted.rstrip(b"\0")


def encode_field(raw_text: str, field_name: str) -> str:
    cipher = encrypt_bytes(raw_text.encode("utf-8"))
    return f"5.{cipher}" if field_name == "f" else cipher


def decode_field(encoded_text: str, field_name: str) -> str:
    cleaned = encoded_text[2:] if field_name == "f" and encoded_text.startswith("5.") else encoded_text
    return decrypt_bytes(cleaned).decode("utf-8", errors="replace")


def generate_qrator_f(timestamp: int | None = None, canvas_hash: int | None = None) -> str:
    ts = timestamp if timestamp is not None else int(time.time())
    ch = canvas_hash if canvas_hash is not None else 2147483647
    raw_f = DEFAULT_BASE_F_TEMPLATE.format(timestamp=ts, canvas_hash=ch, cpu_count=str(os.cpu_count() or 2))
    return encode_field(raw_f, "f")


def build_qrator_multipart(f_value: str, s_value: str, boundary: str | None = None) -> tuple[str, bytes]:
    if boundary:
        b = boundary
        ct = f'multipart/form-data;boundary="{b}"'
    else:
        b = f"----WebKitFormBoundary{secrets.randbelow(10**16):016d}"
        ct = f"multipart/form-data; boundary={b}"
    body = (
        f'--{b}\r\nContent-Disposition: form-data; name="f"\r\n\r\n{f_value}\r\n'
        f'--{b}\r\nContent-Disposition: form-data; name="s"\r\n\r\n{s_value}\r\n'
        f"--{b}--\r\n"
    ).encode("ascii")
    return ct, body


def is_qrator_challenge(response: Any) -> bool:
    if getattr(response, "status_code", 0) not in (302, 429):
        return False
    hdrs = getattr(response, "headers", {}) or {}
    server = str(hdrs.get("server") or hdrs.get("Server") or "").strip().upper()
    if server != "QRATOR":
        return False
    text = getattr(response, "text", "") or ""
    has_meta = bool(re.search(r"<meta\b[^>]*\bhttp-equiv\s*=\s*[\"']refresh[\"']", text, re.I))
    has_script = bool(re.search(r"<script\b[^>]*\bsrc\s*=\s*[\"']/[0-9a-f]{20}\.js[\"']", text, re.I))
    return has_meta and has_script
