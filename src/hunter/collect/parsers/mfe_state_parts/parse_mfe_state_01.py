from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin


def _extract_catalog_items(state: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not state or not isinstance(state, dict):
        return []

    loader_data = state.get("loaderData", {})
    if not isinstance(loader_data, dict):
        return []

    data = loader_data.get("data", {})
    if not isinstance(data, dict):
        return []

    catalog = data.get("catalog", {})
    if not isinstance(catalog, dict):
        return []

    # If Avito explicitly reports 0 exact matches, any returned items are recommendations
    count_val = data.get("count") if data.get("count") is not None else data.get("totalCount")
    if count_val == 0 or (isinstance(count_val, str) and count_val.strip() == "0"):
        return []

    items = catalog.get("items", [])
    if not isinstance(items, list):
        return []
    return items


def _extract_item_url_and_id(item: dict[str, Any], base_url_clean: str) -> tuple[str, str] | None:
    ident = str(item.get("id") or item.get("itemId") or "")
    if not ident.isdigit():
        return None

    url_path = item.get("urlPath") or ""
    if url_path.startswith("http"):
        url = url_path
    elif url_path.startswith("/"):
        url = urljoin(base_url_clean, url_path)
    else:
        url = urljoin(base_url_clean, f"/item/{ident}")
    return ident, url


def _extract_iva_price_payload(iva_obj: Any) -> tuple[Any, Any, Any]:
    if not isinstance(iva_obj, dict):
        return None, None, None
    price_steps = iva_obj.get("PriceStep") or iva_obj.get("priceStep")
    if isinstance(price_steps, list) and price_steps and isinstance(price_steps[0], dict):
        payload = price_steps[0].get("payload", {})
        if isinstance(payload, dict):
            return (
                payload.get("price") or payload.get("value"),
                payload.get("oldPrice") or payload.get("priceWithoutDiscount"),
                payload.get("discountPercent"),
            )
    elif isinstance(price_steps, dict):
        return (
            price_steps.get("price") or price_steps.get("value"),
            price_steps.get("oldPrice") or price_steps.get("priceWithoutDiscount"),
            price_steps.get("discountPercent"),
        )
    return None, None, None


def _extract_mfe_prices(item: dict[str, Any]) -> tuple[int | None, int | None, float | None]:
    price_obj = item.get("priceDetailed") or {}
    price = price_obj.get("value") if isinstance(price_obj, dict) else None
    if price is None and "priceWithoutDiscount" in item:
        price = item.get("priceWithoutDiscount")
    try:
        price = int(price) if price is not None else None
    except (ValueError, TypeError):
        price = None

    old_price = item.get("priceWithoutDiscount")
    try:
        old_price = int(old_price) if old_price is not None else None
    except (ValueError, TypeError):
        old_price = None

    iva_price, iva_old_price, iva_discount_pct = _extract_iva_price_payload(item.get("iva"))
    if iva_price is not None:
        try:
            ip_int = int(iva_price)
            if ip_int > 0:
                if price is not None and ip_int < price:
                    old_price = old_price or price
                price = ip_int
        except (ValueError, TypeError):
            pass

    if "discountPrice" in item:
        try:
            dp = int(item["discountPrice"])
            if dp > 0:
                if price is not None and dp < price:
                    old_price = old_price or price
                price = dp
        except (ValueError, TypeError):
            pass

    if iva_old_price is not None and old_price is None:
        try:
            old_price = int(iva_old_price)
        except (ValueError, TypeError):
            pass

    discount_pct = item.get("discountPercent") or iva_discount_pct
    try:
        discount_pct = float(discount_pct) if discount_pct is not None else None
    except (ValueError, TypeError):
        discount_pct = None

    if old_price is not None and price is not None and old_price > price and not discount_pct:
        discount_pct = round((old_price - price) / old_price * 100, 1)

    return price, old_price, discount_pct


def _extract_mfe_meta(item: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    from hunter.normalize.city import extract_item_city
    city = extract_item_city(item)

    seller_type = None
    seller_obj = item.get("seller") or item.get("iva") or {}
    if isinstance(seller_obj, dict):
        seller_type = str(
            seller_obj.get("kind")
            or seller_obj.get("type")
            or seller_obj.get("sellerType")
            or ""
        ).lower() or None

    sort_ts = item.get("sortTimeStamp")
    published_at = None
    if sort_ts and isinstance(sort_ts, (int, float)):
        try:
            pub_dt = datetime.fromtimestamp(sort_ts / 1000.0, tz=UTC)
            published_at = pub_dt.isoformat()
        except Exception:
            published_at = None

    return str(city) if city else None, seller_type, published_at

