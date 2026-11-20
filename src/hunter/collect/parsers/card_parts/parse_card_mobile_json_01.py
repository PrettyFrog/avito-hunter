from __future__ import annotations

from typing import Any

from hunter.collect.parsers.search import _coerce_price, parse_price

MOBILE_IMAGE_RESOLUTIONS = (
    "1280x723",
    "1170x661",
    "1080x610",
    "828x467",
    "720x407",
)

STUB_URL_PATTERNS = (
    "touch-icon",
    "favicon",
    "stub_avatars",
    "holiday-logo",
    "static/ims",
    "/icons/",
    "logo",
    "avatar",
)


def _is_stub_image_url(url: str) -> bool:
    u = url.lower()
    return any(p in u for p in STUB_URL_PATTERNS)


def _extract_mobile_photos(m: dict[str, Any]) -> list[str]:
    photos: list[str] = []
    for img_dict in m.get("images", []):
        if not isinstance(img_dict, dict):
            continue
        for res in MOBILE_IMAGE_RESOLUTIONS:
            if res in img_dict:
                photos.append(img_dict[res])
                break
        else:
            for v in img_dict.values():
                if isinstance(v, str) and v.startswith("http"):
                    photos.append(v)
                    break
    return photos


def _extract_mobile_price(m: dict[str, Any]) -> int:
    price_val = 0
    raw_p = str(m.get("price", {}).get("value", "0")).replace("\xa0", "").replace(" ", "").strip()
    if raw_p.isdigit():
        price_val = int(raw_p)
    return price_val


def _extract_mobile_seller(m: dict[str, Any]) -> tuple[str | None, str | None]:
    seller = m.get("seller") or {}
    seller_id = str(seller.get("userHash") or seller.get("id") or "") or None
    seller_type = str(seller.get("profileType") or "").lower() or None
    return seller_id, seller_type


def _extract_mobile_params(m: dict[str, Any]) -> dict[str, str]:
    params: dict[str, str] = {}
    raw_params = m.get("parameters") or {}
    if isinstance(raw_params, dict):
        flat_list = raw_params.get("flat") or []
        for p in flat_list:
            if isinstance(p, dict):
                t = p.get("title")
                d = p.get("description")
                if t and d:
                    params[str(t)] = str(d)
    elif isinstance(raw_params, list):
        for p in raw_params:
            if isinstance(p, dict):
                t = p.get("title")
                d = p.get("description")
                if t and d:
                    params[str(t)] = str(d)
    return params


def _node_text(tree: Any, selector: str) -> str | None:
    node = tree.css_first(selector)
    if node is None:
        return None
    text = node.text(separator="\n", strip=True)
    return text or None


def _extract_card_title(tree: Any, data: dict[str, Any]) -> str | None:
    title = data.get("title")
    if isinstance(title, dict):
        title = title.get("text") or title.get("value")
    if not title or title.startswith("Ещё") or title.startswith("Еще") or "фото" in title.lower():
        og = tree.css_first("meta[property='og:title']")
        if og and og.attributes.get("content"):
            title = og.attributes.get("content")
        else:
            title = _node_text(tree, "[data-marker='item-view/title-info'], h1")
    if not title:
        title = _node_text(tree, "[data-marker='item-view/title-info'], h1")
    return title


def _extract_card_prices(
    tree: Any, data: dict[str, Any]
) -> tuple[int | None, int | None, float | None, bool]:
    price_val = _coerce_price(data) if data else None
    parsed = parse_price(price_val) if price_val is not None else None
    if parsed is not None and parsed.price is not None:
        return parsed.price, parsed.old_price, parsed.discount_pct, parsed.price_suspect
    price_node = tree.css_first("[itemprop='price'][content]") or tree.css_first(
        "[data-marker='item-view/item-price']"
    )
    if price_node is not None:
        parsed_dom = parse_price(price_node)
        return (
            parsed_dom.price,
            parsed_dom.old_price,
            parsed_dom.discount_pct,
            parsed_dom.price_suspect,
        )
    return None, None, None, False


def _extract_card_city_and_delivery(
    tree: Any, data: dict[str, Any]
) -> tuple[str | None, bool | None]:
    city = None
    loc = data.get("location") or data.get("geo") or {}
    if isinstance(loc, dict):
        city = loc.get("name")
    city = city or _node_text(tree, "[data-marker='delivery/location'], [itemprop='address']")
    delivery = True if tree.css_first("[data-marker*='delivery']") else None
    return city, delivery


def _collect_raw_card_image_urls(tree: Any, data: dict[str, Any]) -> list[str]:
    raw_urls: list[str] = []
    images = data.get("images") or data.get("imageUrls") or []
    if isinstance(images, list):
        for img in images:
            if isinstance(img, str):
                raw_urls.append(img)
            elif isinstance(img, dict):
                for key in ("1280x960", "640x480", "432x324", "url", "src"):
                    if img.get(key):
                        raw_urls.append(str(img[key]))

    css_sel = (
        "img[src*='img.avito.st/image/'], img[data-src*='img.avito.st/image/'], "
        "img[data-marker*='image'], [data-marker='image-frame/image'] img, "
        "ul[data-marker*='extended-gallery'] img, div[data-marker*='image-frame'] img"
    )
    for node in tree.css(css_sel):
        src = (
            node.attributes.get("src")
            or node.attributes.get("data-src")
            or node.attributes.get("data-url")
        )
        if src:
            raw_urls.append(src)

    if not any("img.avito.st/image/" in u for u in raw_urls):
        for img in tree.css("img"):
            src = img.attributes.get("src") or img.attributes.get("data-src")
            if src and "img.avito.st/image/" in src:
                raw_urls.append(src)

    if not raw_urls:
        og_node = tree.css_first("meta[property='og:image']")
        if og_node and og_node.attributes.get("content"):
            og_src = og_node.attributes.get("content")
            if not _is_stub_image_url(og_src):
                raw_urls.append(og_src)

    return raw_urls


def _filter_and_rank_card_images(raw_urls: list[str]) -> list[str]:
    seen: set[str] = set()
    cdn_photos: list[str] = []
    other_photos: list[str] = []

    for url in raw_urls:
        if url.startswith("//"):
            url = f"https:{url}"
        if not url.startswith("http") or url in seen:
            continue
        seen.add(url)
        if _is_stub_image_url(url):
            continue
        if "img.avito.st/image/" in url or "avito.st/image/" in url:
            cdn_photos.append(url)
        else:
            other_photos.append(url)

    combined = cdn_photos + other_photos
    return combined[:12]
