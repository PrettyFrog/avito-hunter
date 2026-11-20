"""Redirect policy for the anonymous bot's GET/HEAD/POST native requests."""
from types import SimpleNamespace

import pytest


def response(status, target="https://www.avito.ru/next", url="https://www.avito.ru/first"):
    return SimpleNamespace(status_code=status, redirect_url=target, url=url)


@pytest.mark.parametrize("status", [301, 302, 303])
def test_post_to_get_drops_entity_and_params_without_mutating_input(status):
    from hunter.transport.paced_redirects import prepare_paced_request, redirect_step
    original = {"json": {"value": 1}, "params": {"p": 1},
                "headers": {"Content-Type": "application/json", "Content-Length": "11"}}
    prepared = prepare_paced_request("POST", original)
    method, target, kwargs = redirect_step("POST", response(status), prepared)
    assert method == "GET" and target.endswith("/next")
    assert "json" not in kwargs and "params" not in kwargs
    assert kwargs["headers"]["Content-Length"] is None
    assert kwargs["headers"]["Content-Type"] is None
    assert original["json"] == {"value": 1}
    assert original["headers"]["Content-Length"] == "11"


@pytest.mark.parametrize("status", [307, 308])
def test_post_replays_snapshot_of_json_for_preserving_redirects(status):
    from hunter.transport.paced_redirects import prepare_paced_request, redirect_step
    original = {"json": {"nested": [1]}}
    prepared = prepare_paced_request("POST", original)
    original["json"]["nested"].append(2)
    method, _, kwargs = redirect_step("POST", response(status), prepared)
    assert method == "POST" and kwargs["json"] == {"nested": [1]}


def test_cross_origin_disables_auth_and_cookie_headers_case_insensitively():
    from hunter.transport.paced_redirects import prepare_paced_request, redirect_step
    prepared = prepare_paced_request("GET", {"auth": ("synthetic", "secret"),
        "headers": {"aUtHoRiZaTiOn": "synthetic", "COOKIE": "synthetic", "Accept": "application/json"}})
    _, _, kwargs = redirect_step("GET", response(302, target="https://other.test/next"), prepared)
    assert kwargs["headers"]["Authorization"] is None
    assert kwargs["headers"]["Cookie"] is None
    assert kwargs["headers"]["Accept"] == "application/json"
    assert kwargs.get("auth") is None


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_safe_method_is_preserved_and_final_response_stops(method):
    from hunter.transport.paced_redirects import prepare_paced_request, redirect_step
    kwargs = prepare_paced_request(method, {})
    assert redirect_step(method, response(303), kwargs)[0] == method
    assert redirect_step(method, response(200, target=""), kwargs) is None


@pytest.mark.parametrize("kwargs", [{"multipart": object()}, {"cookies": {"u": "token"}},
                                    {"discard_cookies": True}, {"content": iter([b"x"])}])
def test_unreplayable_or_unscoped_options_fail_before_wire(kwargs):
    from hunter.transport.paced_redirects import prepare_paced_request
    with pytest.raises(ValueError):
        prepare_paced_request("POST", kwargs)


def test_redirect_embedded_credentials_or_non_http_scheme_is_rejected():
    from hunter.transport.paced_redirects import redirect_step
    for target in ("https://user:secret@other.test/", "file:///tmp/fixture"):
        with pytest.raises(ValueError):
            redirect_step("GET", response(302, target=target), {})
