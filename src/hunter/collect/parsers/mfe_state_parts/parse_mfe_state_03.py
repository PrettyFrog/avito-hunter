from __future__ import annotations

from typing import Any


def _extract_mfe_photos(item: dict[str, Any], best_photo_url_fn: Any) -> tuple[list[str], int]:
    photos: list[str] = []
    raw_images = item.get("images", [])
    if isinstance(raw_images, list):
        for img_dict in raw_images:
            url_photo = best_photo_url_fn(img_dict)
            if url_photo:
                photos.append(url_photo)

    images_count = item.get("imagesCount", len(photos))
    if isinstance(images_count, (int, float)):
        images_count = int(images_count)
    else:
        images_count = len(photos)
    return photos, images_count


def _extract_mfe_description(item: dict[str, Any]) -> tuple[str, bool]:
    desc = ""
    iva = item.get("iva")
    if isinstance(iva, dict):
        desc_step = iva.get("DescriptionStep")
        if isinstance(desc_step, list) and desc_step:
            payload = desc_step[0].get("payload", {})
            if isinstance(payload, dict):
                desc = payload.get("description", "") or ""

    desc_stripped = desc.strip()
    is_trunc = (
        len(desc_stripped) >= 250
        or desc_stripped.endswith("...")
        or desc_stripped.endswith("…")
    )
    return desc, is_trunc


def _extract_mfe_delivery(item: dict[str, Any]) -> bool | None:
    iva = item.get("iva")
    if not isinstance(iva, dict):
        return None

    for steps in iva.values():
        if not isinstance(steps, list):
            continue
        for step in steps:
            if not isinstance(step, dict):
                continue
            component_data = step.get("componentData")
            if not isinstance(component_data, dict) or component_data.get("component") != "delivery":
                continue
            payload = step.get("payload")
            text = str(payload.get("text") or "").strip().lower() if isinstance(payload, dict) else ""
            if "доставк" not in text:
                continue
            negative = ("недоступ", "нет достав", "без достав", "невозмож", "только самовывоз")
            return not any(marker in text for marker in negative)
    return None


def _build_mfe_extra(
    item: dict[str, Any],
    photos: list[str],
    images_count: int,
    desc: str,
    is_trunc: bool,
    published_at: str | None,
) -> dict[str, Any]:
    rating_obj = item.get("rating")
    rating_score = None
    rating_reviews = None
    if isinstance(rating_obj, dict):
        rating_score = rating_obj.get("score")
        rating_reviews = rating_obj.get("summary")

    is_reserved = bool(item.get("isReserved", False))
    is_promoted = bool(
        item.get("isVas")
        or item.get("isPromoted")
        or item.get("vas")
        or item.get("isVip")
        or item.get("vip")
        or item.get("badge")
    )

    extra: dict[str, Any] = {
        "description_preview": desc,
        "desc_truncated": is_trunc,
        "photos": photos,
        "images_count": images_count,
        "rating": rating_obj if isinstance(rating_obj, dict) else None,
        "rating_score": rating_score,
        "rating_reviews": rating_reviews,
        "published_at": published_at,
        "reserved_via_delivery": is_reserved,
        "is_promoted": is_promoted,
    }
    delivery = _extract_mfe_delivery(item)
    if delivery is not None:
        extra["delivery"] = delivery
    return extra
