from __future__ import annotations

from datetime import UTC, datetime


def format_publication_delay(
    published_at_str: str | None, ref_dt: datetime | None = None
) -> str | None:
    """Returns exact human-readable latency: e.g. '42с', '1м 24с', '2ч 15м 3с', '1д 4ч 12м'."""
    if not published_at_str:
        return None
    try:
        pub_dt = datetime.fromisoformat(published_at_str.replace("Z", "+00:00"))
        if pub_dt.tzinfo is None:
            pub_dt = pub_dt.replace(tzinfo=UTC)
        now = ref_dt or datetime.now(UTC)
        diff_sec = max(0, int((now - pub_dt).total_seconds()))
        if diff_sec < 60:
            return f"{diff_sec}с"
        minutes = diff_sec // 60
        seconds = diff_sec % 60
        if minutes < 60:
            return f"{minutes}м {seconds:02d}с"
        hours = minutes // 60
        rem_min = minutes % 60
        if hours < 24:
            return f"{hours}ч {rem_min:02d}м {seconds:02d}с"
        days = hours // 24
        rem_hours = hours % 24
        return f"{days}д {rem_hours}ч {rem_min:02d}м"
    except Exception:
        return None
