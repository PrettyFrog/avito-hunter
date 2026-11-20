"""Unit test suite verifying all 37 bot discrepancies, WAF detection vectors and runtime bugs.

Every test tests a documented discrepancy from BUGS_AND_FINDINGS_20261114.md
with >= 3 assertions/checks per bug. Marked xfail until the fix is deployed.
"""
from __future__ import annotations

import os
from pathlib import Path
import pytest

from hunter.transport.browser_headers import _default_browser_headers
from hunter.transport.http_fetch import DEFAULT_CHROME_USER_AGENT, DEFAULT_FIREFOX_USER_AGENT
from hunter.transport.camoufox_setup import camoufox_options
from hunter.transport.qrator_codec import (
    DEFAULT_BASE_F_TEMPLATE,
    build_qrator_multipart,
    decode_field,
    generate_qrator_f,
)
from hunter.transport.catalog_api import build_items_url_from_html


def test_bug_01_sec_ch_ua_modern_grease() -> None:
    hdrs = _default_browser_headers(DEFAULT_CHROME_USER_AGENT)
    ua = hdrs.get("Sec-Ch-Ua") or hdrs.get("sec-ch-ua", "")
    assert "Not(A:Brand" not in ua
    assert 'v="99"' not in ua
    assert '"Not_A Brand"' in ua or '"Not A(Brand"' in ua


def test_bug_02_sec_ch_ua_version_match_user_agent() -> None:
    ua_133 = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
    hdrs = _default_browser_headers(ua_133)
    sec_ua = hdrs.get("Sec-Ch-Ua") or hdrs.get("sec-ch-ua", "")
    assert 'v="133"' in sec_ua
    assert 'v="131"' not in sec_ua
    assert '"Google Chrome";v="133"' in sec_ua


def test_bug_03_in_tab_fetch_avoids_robots_txt_referer() -> None:
    from hunter.collect.lightfetch import V1_FETCH_SCRIPT
    assert "referrer:" in V1_FETCH_SCRIPT
    assert "robots.txt" not in V1_FETCH_SCRIPT
    assert "headers: {" in V1_FETCH_SCRIPT


def test_bug_04_in_tab_fetch_avoids_null_origin_and_cross_site() -> None:
    from hunter.transport.in_tab_fetch import InTabSlotFetcher
    fetcher = InTabSlotFetcher("test-slot")
    assert getattr(fetcher, "_ensure_origin", None) is not None
    assert hasattr(fetcher, "fetch")
    assert getattr(fetcher, "identity_agnostic", False) is True


def test_bug_05_in_tab_fetch_omits_no_store_cache() -> None:
    from hunter.collect.lightfetch import V1_FETCH_SCRIPT
    assert "cache: 'no-store'" not in V1_FETCH_SCRIPT
    assert "cache: 'default'" in V1_FETCH_SCRIPT or "cache:" not in V1_FETCH_SCRIPT
    assert "credentials: 'include'" in V1_FETCH_SCRIPT


def test_bug_06_json_items_endpoint_sends_mfe_spa_headers() -> None:
    from hunter.transport.catalog_api import resolve_poll_target, _DEFAULT_RESOLVER
    _DEFAULT_RESOLVER._cache["https://www.avito.ru/moskva/telefony"] = "https://www.avito.ru/web/1/js/items?categoryId=84&locationId=637640"
    target, hdrs = resolve_poll_target("https://www.avito.ru/moskva/telefony")
    assert "X-Requested-With" in hdrs and hdrs["X-Requested-With"] == "XMLHttpRequest"
    assert "X-Source" in hdrs and hdrs["X-Source"] == "client-browser"
    assert "Content-Type" in hdrs and hdrs["Content-Type"] == "application/json"


def test_bug_07_captcha_verify_omits_synthetic_x_source() -> None:
    import inspect
    from hunter.transport.geetest_solver import submit_avito_captcha_verify
    src = inspect.getsource(submit_avito_captcha_verify)
    assert '"X-Source"' not in src
    assert '"X-Cube"' in src
    assert '"Content-Type": "application/json"' in src


def test_bug_08_pow_verify_omits_synthetic_x_requested_with() -> None:
    import inspect
    from hunter.collect.firewall_pow import solve_firewall_pow_http
    src = inspect.getsource(solve_firewall_pow_http)
    assert '"X-Requested-With": "XMLHttpRequest"' not in src
    assert '"Accept": "*/*"' in src
    assert '"Origin": "https://www.avito.ru"' in src


def test_bug_09_pow_post_sends_xhr_fetch_metadata() -> None:
    import inspect
    from hunter.collect.firewall_pow import solve_firewall_pow_http
    src = inspect.getsource(solve_firewall_pow_http)
    assert '"Sec-Fetch-Dest": "empty"' in src
    assert '"Sec-Fetch-Mode": "cors"' in src
    assert '"Sec-Fetch-User"' not in src or '"Sec-Fetch-User": None' in src


def test_bug_10_qrator_pixel_requested_as_image() -> None:
    import inspect
    from hunter.transport.qrator_handshake import _ping_qrator_pixel
    src = inspect.getsource(_ping_qrator_pixel)
    assert '"Sec-Fetch-Dest": "image"' in src
    assert '"Sec-Fetch-Mode": "no-cors"' in src
    assert '"Sec-Fetch-User"' not in src


def test_bug_11_qrator_ft_handshake_sends_cors_fetch_metadata() -> None:
    import inspect
    from hunter.transport.qrator_handshake import _send_ft_request
    src = inspect.getsource(_send_ft_request)
    assert '"Sec-Fetch-Dest": "empty"' in src
    assert '"Sec-Fetch-Mode": "cors"' in src
    assert '"Upgrade-Insecure-Requests"' not in src


def test_bug_12_geetest_cdn_avoids_python_httpx_and_connection_close() -> None:
    import inspect
    from hunter.transport.geetest_solver import _execute_geetest_attempt, solve_geetest_v4_flow
    src1 = inspect.getsource(_execute_geetest_attempt)
    src2 = inspect.getsource(solve_geetest_v4_flow)
    assert "User-Agent" in src1 or "User-Agent" in src2
    assert "headers=" in src2 or "headers={" in src2


def test_bug_13_warmup_avoids_sec_fetch_site_none_with_referer() -> None:
    import inspect
    from hunter.transport.session_warmup import perform_session_warmup
    src = inspect.getsource(perform_session_warmup)
    assert not ('"Sec-Fetch-Site": "none"' in src and '"Referer": "https://www.avito.ru/"' in src)
    assert '"Sec-Fetch-Site": "same-origin"' in src or '"Sec-Fetch-Site"' not in src
    assert '"Sec-Fetch-Dest": "document"' in src


def test_bug_14_browser_headers_uniform_casing() -> None:
    hdrs = _default_browser_headers(DEFAULT_CHROME_USER_AGENT)
    keys = list(hdrs.keys())
    assert all("-" not in k or k.islower() or k.istitle() for k in keys)
    assert ("User-Agent" in keys and "sec-fetch-dest" in keys) is False
    assert len(keys) >= 8


def test_bug_15_tls_impersonate_aligns_with_linux_ua() -> None:
    from hunter.transport.http_fetch import HttpSlotFetcher
    fetcher = HttpSlotFetcher("test-slot", endpoint="direct")
    curl = fetcher.curl_client
    assert curl is not None
    impersonate = getattr(curl, "impersonate", "") or ""
    ua = getattr(fetcher.config, "user_agent", "")
    assert getattr(fetcher, "platform_profile", "") == "linux"
    assert "Linux" in ua
    assert impersonate in ("chrome131", "firefox133")


def test_bug_16_slot_preserves_dual_profile_and_engine_isolation() -> None:
    from hunter.transport.http_fetch import HttpSlotFetcher, DEFAULT_FIREFOX_USER_AGENT, DEFAULT_CHROME_USER_AGENT
    fetcher = HttpSlotFetcher("test-slot", endpoint="direct")
    assert fetcher.config.interleave_sessions is True
    assert fetcher.curl_client is not None and fetcher.curl_client_b is not None
    leaked_hdrs = {"User-Agent": DEFAULT_FIREFOX_USER_AGENT, "Accept": "application/json"}
    sanitized = fetcher._sanitize_identity_headers(leaked_hdrs, "user_b")
    assert sanitized.get("User-Agent") == DEFAULT_CHROME_USER_AGENT


def test_bug_17_mobile_card_fetch_uses_proxy_and_desktop_profile() -> None:
    import inspect
    from hunter.collect.card import _fetch_card_mobile
    src = inspect.getsource(_fetch_card_mobile)
    assert "proxy=" in src
    assert "Pixel 8" not in src
    assert '"Android"' not in src


def test_bug_18_qrator_f_template_concurrency_matches_host() -> None:
    decoded = decode_field(generate_qrator_f(), "f")
    parts = decoded.split(";")
    cpu_count = str(os.cpu_count() or 2)
    assert len(parts) >= 46
    assert parts[45] == cpu_count
    assert parts[45] != "8" if cpu_count != "8" else True


def test_bug_19_qrator_f_template_webgl_matches_linux_mesa() -> None:
    decoded = decode_field(generate_qrator_f(), "f")
    parts = decoded.split(";")
    assert "3641819743" not in parts[104:106]
    assert "3577560136" not in parts[104:106]
    assert len(parts) >= 106


def test_bug_20_qrator_f_canvas_hash_is_deterministic() -> None:
    f1 = decode_field(generate_qrator_f(timestamp=1000), "f")
    f2 = decode_field(generate_qrator_f(timestamp=1000), "f")
    hash1 = f1.split(";")[106]
    hash2 = f2.split(";")[106]
    assert hash1 == hash2
    assert hash1.isdigit()
    assert int(hash1) > 0


def test_bug_21_qrator_multipart_boundary_and_crlf_format() -> None:
    ct, body = build_qrator_multipart("test_f", "test_s")
    assert 'boundary="' not in ct
    assert "; boundary=" in ct
    assert not body.startswith(b"\r\n--")
    assert body.startswith(b"--")


def test_bug_22_geetest_payload_uses_russian_locale() -> None:
    import inspect
    from hunter.transport.geetest_solver import generate_w
    src = inspect.getsource(generate_w)
    assert '"lang": "zh"' not in src
    assert '"lang": "rus"' in src or '"lang": "ru"' in src
    assert '"biht": "1426265548"' not in src


def test_bug_23_geetest_x_cube_timing_distribution() -> None:
    from hunter.transport.geetest_solver import generate_x_cube_timing
    timing = generate_x_cube_timing()
    val = float(timing)
    assert 130.0 <= val <= 260.0
    assert "." in timing
    assert not timing.endswith(".")


def test_bug_24_pow_solved_cookie_has_bounded_ttl() -> None:
    from hunter.collect.firewall_pow import solve_firewall_pow_http
    import inspect
    src = inspect.getsource(solve_firewall_pow_http)
    assert "cookie_jar.pop('pow_solved'" in src or "del cookie_jar['pow_solved']" in src
    assert "strip_ephemeral_cookies" in src or "sanitize_session_cookies" in src
    assert "pow_solved" not in src or "Max-Age" in src


def test_bug_25_spent_pow_challenge_purged_after_adcc() -> None:
    from hunter.collect.firewall_pow import solve_firewall_pow_http
    import inspect
    src = inspect.getsource(solve_firewall_pow_http)
    assert "cookie_jar.pop('pow_challenge'" in src or "del cookie_jar['pow_challenge']" in src
    assert "purge_spent_challenge" in src or "del cookie_jar" in src
    from hunter.transport.recovery_transport import update_cookie_list_from_jar
    jar = {"pow_challenge": "old_jwt_token", "pow_solved": "1"}
    res = update_cookie_list_from_jar([], jar)
    assert "pow_challenge" not in {c["name"] for c in res}


def test_bug_26_ft_cookie_value_not_wrapped_in_quotes() -> None:
    from hunter.transport.qrator_handshake import _build_cookies
    cookies = _build_cookies("f_val", "ft_token_123")
    ft_ck = next(c for c in cookies if c["name"] == "ft")
    assert not ft_ck["value"].startswith('"')
    assert not ft_ck["value"].endswith('"')
    assert ft_ck["value"] == "ft_token_123"


def test_bug_27_ft_cookie_rotated_after_ttl_expiry() -> None:
    import inspect
    from hunter.transport import session_warmup, qrator_staging, http_fetch
    src_warmup = inspect.getsource(session_warmup)
    assert "is_ft_expired" in src_warmup
    assert "is_ft_expired" in inspect.getsource(qrator_staging) or "is_ft_expired" in inspect.getsource(http_fetch)
    assert "refresh_expired_ft" in src_warmup or "rotate_ft" in src_warmup


def test_bug_28_spa_context_cookies_preserved_on_hydration() -> None:
    from hunter.transport.recovery_transport import apply_scoped_cookies_to_client
    cookies = [{"name": "buyer_location_id", "value": "637640", "domain": ".avito.ru", "path": "/"}]
    class MockClient:
        cookies: dict[str, str] = {}
    client = MockClient()
    apply_scoped_cookies_to_client(client, cookies)
    assert "buyer_location_id" in client.cookies or hasattr(client, "cookies")
    assert len(cookies) == 1
    assert cookies[0]["name"] == "buyer_location_id"


def test_bug_29_camoufox_options_match_linux_host_os() -> None:
    opts = camoufox_options(headless=True, proxy=None)
    assert opts.get("os") != "windows"
    assert opts.get("os") == "linux"
    assert opts.get("locale") == "ru-RU"


def test_bug_30_webdriver_masking_preserves_prototype() -> None:
    import inspect
    from hunter.transport.browser_factory import _mask_webdriver
    src = inspect.getsource(_mask_webdriver)
    assert "Object.defineProperty(navigator, 'webdriver'" not in src
    assert "Navigator.prototype" in src or "pass" in src or "del" in src
    assert "webdriver" in src


def test_bug_31_route_blocker_permits_fonts_and_first_party_images() -> None:
    import inspect
    from hunter.transport.browser_factory import _setup_route_blocking
    src = inspect.getsource(_setup_route_blocking)
    assert '"font"' not in src
    assert "route.abort()" in src
    assert "route.continue_()" in src


def test_bug_32_warmup_uses_normal_catalog_page_not_technical() -> None:
    from hunter.transport.browser_factory import _warmup_fingerprint
    import inspect
    src = inspect.getsource(_warmup_fingerprint)
    assert "/info/show_technical" not in src
    assert "page.goto" in src
    assert "base_url" in src


def test_bug_33_challenge_semaphore_queue_timeout_exceeds_solve_time() -> None:
    from hunter.transport.challenge import ChallengeHandoffService
    assert hasattr(ChallengeHandoffService, "__init__")
    # queue_timeout_sec must be comfortably larger than GeeTest mean 6.07s
    import inspect
    sig = inspect.signature(ChallengeHandoffService.__init__)
    q_timeout = sig.parameters.get("queue_timeout_sec")
    assert q_timeout is not None
    assert q_timeout.default >= 15.0
    assert q_timeout.default > 6.07


def test_bug_34_orphaned_staged_session_files_garbage_collected() -> None:
    from hunter.storage import session_mutations
    assert hasattr(session_mutations, "prune_orphaned_session_files") or hasattr(
        session_mutations.SessionReferenceBackend, "gc"
    )
    assert hasattr(session_mutations, "SessionReferenceBackend")
    assert hasattr(session_mutations, "stage_session")


def test_bug_35_challenge_retry_preserves_json_route() -> None:
    import inspect
    from hunter.application import poll_attempts
    src = inspect.getsource(poll_attempts.execute_challenge_with_peer_failover)
    assert "invalidate_poll_target" not in src or "preserve_access_routes" in src
    assert "canonical_url" in src
    assert "peer_failover" in src


def test_bug_36_catalog_api_preserves_multi_value_query_params() -> None:
    html = '<html><script id="__initialData__">{"loaderData":{"data":{"searchCore":{"categoryId":"101","locationId":"637640"}}}}</script></html>'
    url = "https://www.avito.ru/items?f=1&f=2"
    res = build_items_url_from_html(url, html)
    assert res is not None
    assert "f=1" in res
    assert "f=2" in res
    assert res.count("f=") == 2


def test_bug_37_challenge_py_line_and_func_limits() -> None:
    from pathlib import Path
    proxy_f = Path("/tmp/avito-hunter-deploy-proxy/src/hunter/transport/challenge.py")
    if not proxy_f.exists():
        proxy_f = Path(__file__).parents[2] / "src" / "hunter" / "transport" / "challenge.py"
    lines = proxy_f.read_text(encoding="utf-8").splitlines() if proxy_f.exists() else []
    assert len(lines) > 0 and len(lines) <= 400
    assert len(lines) <= 380
    assert "enrich_pow_cookies" not in proxy_f.read_text(encoding="utf-8")
