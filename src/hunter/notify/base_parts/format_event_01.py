"""Leaf formatting helpers for NotificationEvent (T-072)."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hunter.core.models import NotificationEvent

from hunter.notify.helpers import format_publication_delay


def _build_model_line(event: NotificationEvent, is_gem: bool) -> str | None:
    if is_gem:
        title_lower = (event.title or "").lower()
        model_str = (event.model_id or "").lower()
        common_brands = {
            "bose",
            "sony",
            "apple",
            "sennheiser",
            "dyson",
            "garmin",
            "samsung",
            "xiaomi",
            "google",
            "anker",
            "audio-technica",
            "beyerdynamic",
        }
        model_tokens = [
            part
            for part in model_str.replace("-", "_").split("_")
            if len(part) >= 3
            and part not in common_brands
            and part not in ("gen", "series", "edition", "headphones", "wireless")
        ]
        has_model_in_title = (
            any(token in title_lower for token in model_tokens)
            if model_tokens
            else (model_str in title_lower)
        )
        if not has_model_in_title:
            return "Модель в объявлении не указана"
        if event.model_id:
            return f"модель: {event.model_id}"
        return None
    if event.model_id:
        return f"модель: {event.model_id}"
    return None


def _append_meta_lines(lines: list[str], event: NotificationEvent) -> None:
    # Standardized photo check line (PH-1)
    photo_check_line = None
    for r in event.reasons:
        if r.startswith("Проверка по фото:"):
            photo_check_line = r
            break
    if not photo_check_line and "⚠ фото не проверены" in event.reasons:
        photo_check_line = "Проверка по фото: не проверено"

    if photo_check_line and photo_check_line not in lines:
        lines.append(photo_check_line)

    # For watch verdict (default in new/shadow), do not display market fields (fair price/median)
    if event.verdict != "watch" and event.discount is not None and event.discount > 0:
        ev_str = f" (выгода ~{event.ev:,} ₽)".replace(",", " ") if event.ev else ""
        lines.append(f"💰 скидка {round(event.discount * 100)}%{ev_str}")

    seller_info = getattr(event, "seller_type", None)
    if seller_info:
        lines.append(f"продавец: {seller_info}")

    age_info = getattr(event, "age_text", None)
    if age_info:
        lines.append(f"возраст: {age_info}")

    if hasattr(event, "specs") and event.specs:
        bh = event.specs.get("battery_health")
        cyc = event.specs.get("battery_cycles")
        if bh:
            b_line = f"🔋 АКБ: {bh}" + (f" ({cyc} циклов)" if cyc else "")
            if event.specs.get("battery_degraded"):
                b_line = f"⚠️ Износ АКБ: {bh}" + (f" ({cyc} циклов)" if cyc else "")
            if b_line not in lines:
                lines.append(b_line)

    delay_str = format_publication_delay(getattr(event, "published_at", None))
    if delay_str:
        lines.append(f"⏱ Задержка от публикации: {delay_str}")


def _append_reason_lines(lines: list[str], event: NotificationEvent) -> None:
    # Soft vision mismatch warning, photo warnings, and loose candidate status (A6)
    for r in event.reasons:
        if "по фото похоже на другую модель" in r:
            if r not in lines:
                lines.append(r)
        elif r.startswith("⚠ по фото") or r.startswith("🔎 кандидат"):
            if r not in lines:
                lines.append(r)

    # Photo check warning
    if "⚠ фото не проверены" in event.reasons:
        if "⚠ фото не проверены" not in lines:
            lines.append("⚠ фото не проверены")

    for r in event.reasons:
        if r.startswith("gem_reason:"):
            lines.append(f"💡 {r[len('gem_reason:') :]}")
        elif r.startswith("red_flag:"):
            lines.append(f"🚩 {r}")
