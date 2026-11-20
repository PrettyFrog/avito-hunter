"""Unit tests for response classification and health signals (S5 / T-480).

Covers:
- SDK captcha mention + catalog feed = OK (not challenge).
- Challenge barrier without feed = CHALLENGE.
- Explicit empty markers (MFE count=0 or catalog-empty) = EXPLICIT_EMPTY.
- UNKNOWN outcome does not update seen state.
- HTTP 403 / 429 classification = BLOCKED.
- Cookie presence + HTTP 200 != auth healthy (detects unauthenticated session).
- Authenticated fixtures vs logout fixtures.
"""

from __future__ import annotations

import json

from hunter.domain.slots import SlotSignal
from hunter.transport.classification import (
    ResponseOutcome,
    classify_response,
)


def _make_mfe_html(
    items: list[dict] | None = None,
    count: int | None = None,
    user_auth: bool | None = None,
    extra_html: str = "",
) -> str:
    loader_data: dict = {"data": {"catalog": {"items": items or []}}}
    if count is not None:
        loader_data["data"]["count"] = count
    state: dict = {"loaderData": loader_data}
    if user_auth is not None:
        state["user"] = {"isAuth": user_auth, "id": 99999 if user_auth else 0}
    mfe_str = json.dumps(state)
    return (
        f"<!DOCTYPE html><html><head>{extra_html}</head><body>"
        f'<script data-mfe-state="true">{mfe_str}</script>'
        f"</body></html>"
    )


def test_sdk_captcha_mention_with_feed_is_ok():
    item = {"id": "1001", "title": "Sony Headphones", "priceDetailed": {"value": 25000}}
    extra = '<script src="https://smartcaptcha.yandexcloud.net/captcha.js"></script>'
    html = _make_mfe_html(items=[item], extra_html=extra)

    res = classify_response(200, html)
    assert res.outcome == ResponseOutcome.OK
    assert res.is_healthy is True
    assert res.should_update_seen is True
    assert res.suggested_signal == SlotSignal.PROBE_OK
    assert len(res.hits) == 1
    assert res.hits[0].avito_id == "1001"


def test_challenge_without_feed_is_challenge():
    html = """
    <!DOCTYPE html>
    <html><body>
      <div id="captcha-wrapper" data-marker="captcha-box">
        <h3>Доступ ограничен: проблема с IP</h3>
      </div>
    </body></html>
    """
    res = classify_response(200, html)
    assert res.outcome == ResponseOutcome.CHALLENGE
    assert res.is_healthy is False
    assert res.should_update_seen is False
    assert res.suggested_signal == SlotSignal.CHALLENGE_DETECTED
    assert len(res.hits) == 0


def test_empty_marker_is_explicit_empty():
    # 1. MFE state with count: 0
    html_mfe = _make_mfe_html(items=[], count=0)
    res_mfe = classify_response(200, html_mfe)
    assert res_mfe.outcome == ResponseOutcome.EXPLICIT_EMPTY
    assert res_mfe.is_healthy is True
    assert res_mfe.should_update_seen is True
    assert res_mfe.suggested_signal == SlotSignal.PROBE_OK

    # 2. DOM empty marker
    html_dom = (
        '<html><body><div data-marker="catalog-empty">'
        "<h3>Ничего не найдено</h3></div></body></html>"
    )
    res_dom = classify_response(200, html_dom)
    assert res_dom.outcome == ResponseOutcome.EXPLICIT_EMPTY
    assert res_dom.is_healthy is True
    assert res_dom.should_update_seen is True


def test_unknown_does_not_clear_seen():
    html_unknown = "<html><body><h1>Maintenance Page</h1><p>Under construction</p></body></html>"
    res = classify_response(200, html_unknown)
    assert res.outcome == ResponseOutcome.UNKNOWN
    assert res.is_healthy is False
    # CRITICAL: unknown must never update/clear seen state
    assert res.should_update_seen is False
    assert res.suggested_signal is None


def test_http_403_and_429_blocked():
    res_403 = classify_response(403, "<html>403 Forbidden</html>")
    assert res_403.outcome == ResponseOutcome.BLOCKED
    assert res_403.is_healthy is False
    assert res_403.suggested_signal == SlotSignal.HTTP_403

    res_429 = classify_response(429, "<html>429 Too Many Requests</html>")
    assert res_429.outcome == ResponseOutcome.BLOCKED
    assert res_429.suggested_signal == SlotSignal.HTTP_429


def test_http_429_with_challenge_marker_is_challenge():
    html_challenge = "<html><head><title>Доступ ограничен: проблема с IP</title></head></html>"
    res = classify_response(429, html_challenge)
    assert res.outcome == ResponseOutcome.CHALLENGE
    assert res.suggested_signal == SlotSignal.CHALLENGE_DETECTED


def test_auth_required_detected_when_unauthenticated_despite_http_200():
    # Case 1: MFE state explicitly reports isAuth=False
    item = {"id": "1002", "title": "Bose QC45", "priceDetailed": {"value": 18000}}
    html = _make_mfe_html(items=[item], user_auth=False)
    res = classify_response(200, html, require_auth=True)
    assert res.outcome == ResponseOutcome.AUTH_REQUIRED
    assert res.is_healthy is False
    assert res.is_authenticated is False
    assert res.suggested_signal == SlotSignal.AUTH_INVALID

    # Case 2: Redirected to passport/login
    res_login = classify_response(200, "<html>login</html>", url="https://passport.avito.ru/login")
    assert res_login.outcome == ResponseOutcome.AUTH_REQUIRED
    assert res_login.suggested_signal == SlotSignal.AUTH_INVALID


def test_authenticated_fixture_with_feed():
    item = {"id": "1003", "title": "Sony XM4", "priceDetailed": {"value": 22000}}
    html = _make_mfe_html(items=[item], user_auth=True)
    res = classify_response(200, html, require_auth=True)
    assert res.outcome == ResponseOutcome.OK
    assert res.is_healthy is True
    assert res.is_authenticated is True
    assert res.suggested_signal == SlotSignal.PROBE_OK
    assert len(res.hits) == 1


def test_unauthenticated_public_feed_is_ok_when_require_auth_is_false():
    item = {"id": "1004", "title": "Bose QC Ultra", "priceDetailed": {"value": 25000}}
    html = _make_mfe_html(items=[item], user_auth=False)
    res = classify_response(200, html, require_auth=False)
    assert res.outcome == ResponseOutcome.OK
    assert res.is_healthy is True
    assert res.is_authenticated is False
    assert res.suggested_signal == SlotSignal.PROBE_OK
    assert len(res.hits) == 1


def test_http_439_pow_challenge_detected():
    html_pow = "<html><head><title>Доступ ограничен: проверка безопасности</title></head><body><div class='firewall__title'>Нужна проверка</div></body></html>"
    res = classify_response(439, html_pow)
    assert res.outcome == ResponseOutcome.CHALLENGE
    assert res.suggested_signal == SlotSignal.CHALLENGE_DETECTED


