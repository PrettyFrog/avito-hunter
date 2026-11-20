from __future__ import annotations

import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from hunter.domain.observations import ParseOutcome
from hunter.transport.catalog_api import (
    CatalogUrlResolver,
    build_items_url_from_html,
)


def test_build_items_url_from_html_extracts_search_core():
    payload = {
        "loaderData": {
            "data": {
                "searchCore": {
                    "categoryId": 84,
                    "locationId": 621540,
                    "rootCategoryId": 6,
                    "verticalCategoryId": 4,
                    "params": {"110618": 469735, "110680": 458500},
                }
            }
        }
    }
    html = f'<html><head><script data-mfe-state="true">{json.dumps(payload)}</script></head><body></body></html>'
    canon = "https://www.avito.ru/all/telefony/apple?s=104&pmax=100000"
    url = build_items_url_from_html(canon, html)
    assert url is not None
    assert "https://www.avito.ru/web/1/js/items?" in url
    assert "categoryId=84" in url
    assert "locationId=621540" in url
    assert "s=104" in url
    assert "pmax=100000" in url
    assert "params%5B110618%5D=469735" in url or "params[110618]=469735" in url
    assert "updateListOnly=true" in url


def test_catalog_url_resolver_cached_target_resolves():
    resolver = CatalogUrlResolver(enabled=True)
    known = (
        "https://www.avito.ru/all/telefony/mobilnye_telefony/apple-ASgBAgICAkS0wA3OqzmwwQ2I_Dc"
        "?cd=1&f=ASgBAgECAkS0wA3OqzmwwQ2I_DcBRcaaDBZ7ImZyb20iOjAsInRvIjoxMDAwMDB9&s=104&pmax=1000000"
    )
    items_url = "https://www.avito.ru/web/1/js/items?categoryId=84&locationId=621540"
    resolver._cache[known] = items_url
    target, hdrs = resolver.resolve_poll_target(known)
    assert "/web/1/js/items" in target
    assert "categoryId=84" in target
    assert hdrs["Accept"] == "application/json, text/plain, */*"
    assert hdrs["Referer"] == known


def test_catalog_url_resolver_bootstrap_html_and_transition():
    resolver = CatalogUrlResolver(enabled=True)
    new_query = "https://www.avito.ru/moskva/avtomobili?s=104"
    target, hdrs = resolver.resolve_poll_target(new_query)
    assert target == new_query
    assert "text/html" in hdrs["Accept"]

    payload = {"loaderData": {"data": {"searchCore": {"categoryId": 9, "locationId": 637640}}}}
    sample_html = f'<script data-mfe-state="true">{json.dumps(payload)}</script>'
    mapped = resolver.record_poll_response(new_query, sample_html, 200)
    assert mapped is not None
    assert "/web/1/js/items?" in mapped
    assert "categoryId=9" in mapped
    assert "locationId=637640" in mapped

    target2, hdrs2 = resolver.resolve_poll_target(new_query)
    assert target2 == mapped
    assert hdrs2["Accept"] == "application/json, text/plain, */*"
    assert hdrs2["Referer"] == new_query


def test_catalog_url_resolver_invalidate_on_404():
    resolver = CatalogUrlResolver(enabled=True)
    query = "https://www.avito.ru/moskva/avtomobili?s=104"
    resolver._cache[query] = "https://www.avito.ru/web/1/js/items?categoryId=9"
    resolver.record_poll_response(query, "not found", 404)
    target, hdrs = resolver.resolve_poll_target(query)
    assert target == query
    assert "text/html" in hdrs["Accept"]


@pytest.mark.parametrize("status", [403, 429, 439])
def test_access_failure_preserves_confirmed_json_route(status):
    resolver = CatalogUrlResolver(enabled=True, preserve_access_routes=True)
    query = "https://www.avito.ru/moskva/avtomobili?s=104"
    target = "https://www.avito.ru/web/1/js/items?categoryId=9"
    resolver._cache[query] = target
    assert resolver.record_poll_response(query, '{"error":"access denied"}', status) is None
    assert resolver.resolve_poll_target(query)[0] == target


@pytest.mark.parametrize("body", [
    '<html>pow_challenge "categoryId":84,"locationId":637640</html>',
    '<html><script>window.location.href="/firewall/captcha";</script>'
    '"categoryId":84,"locationId":637640</html>',
    '<html><script>window.location.href="/redirect";</script>'
    '"categoryId":84,"locationId":637640</html>',
    '{"status":"error","categoryId":84,"locationId":637640}',
    '{"broken":',
])
def test_error_body_does_not_teach_or_replace_route(body):
    resolver = CatalogUrlResolver(enabled=True)
    query = "https://www.avito.ru/moskva?q=test"
    assert resolver.record_poll_response(query, body, 200) is None
    assert resolver.resolve_poll_target(query)[0] == query
    resolver._cache[query] = "https://www.avito.ru/web/1/js/items?categoryId=9"
    assert resolver.record_poll_response(query, body, 200) is None
    assert resolver.resolve_poll_target(query)[0].endswith("categoryId=9")


def test_gone_route_is_invalidated():
    resolver = CatalogUrlResolver(enabled=True)
    query = "https://www.avito.ru/moskva?q=test"
    resolver._cache[query] = "https://www.avito.ru/web/1/js/items?categoryId=9"
    resolver.record_poll_response(query, "gone", 410)
    assert resolver.resolve_poll_target(query)[0] == query


@pytest.mark.parametrize("enabled", [False, True])
def test_route_policy_can_be_enabled_and_rolled_back(monkeypatch, enabled):
    monkeypatch.setenv("HUNTER_PRESERVE_JSON_ROUTES", "1" if enabled else "0")
    resolver = CatalogUrlResolver(enabled=True)
    query, target = "https://www.avito.ru/moskva?q=test", "https://www.avito.ru/web/1/js/items?categoryId=9"
    resolver._cache[query] = target
    resolver.record_poll_response(query, "denied", 429)
    assert resolver.resolve_poll_target(query)[0] == (target if enabled else query)
    resolver._cache[query] = target
    resolver.invalidate_ambiguous(query)
    assert resolver.resolve_poll_target(query)[0] == (target if enabled else query)
