"""Conservative encoded-body accounting for decoded curl streams."""

from collections.abc import Mapping


def declared_wire_body_bytes(headers: Mapping[str, str]) -> int | None:
    """Use Content-Length only after consuming a complete, non-chunked body.

    This is the server-declared encoded body length, excluding transport/header
    overhead. Curl's streaming download_size is an early snapshot, not a total.
    Unknown length must remain unknown rather than use decoded chunk sizes.
    """
    normalized = {str(key).lower(): str(value) for key, value in headers.items()}
    if "transfer-encoding" in normalized:
        return None
    value = normalized.get("content-length", "").strip()
    if not value.isascii() or not value.isdecimal() or len(value) > 20:
        return None
    return int(value)
