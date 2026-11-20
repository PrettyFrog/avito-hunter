from __future__ import annotations

import base64
import hashlib
import json
import re
from typing import Any
from loguru import logger
from hunter.transport.metered_http import HttpDeferred


def find_nonce(
    challenge_id: str, complexity: int = 4, max_iter: int = 10_000_000, stop_event: Any = None,
) -> int | None:
    """Finds a nonce such that sha256(f"{challenge_id}:{nonce}") starts with '0' * complexity."""
    target_prefix = "0" * max(1, min(complexity, 8))
    id_bytes = challenge_id.encode("ascii", errors="replace")
    nonce, chunk = 0, 5000
    while nonce < max_iter:
        if stop_event is not None and stop_event.is_set():
            return None
        end = min(nonce + chunk, max_iter)
        while nonce < end:
            h = hashlib.sha256(b"%b:%d" % (id_bytes, nonce)).hexdigest()
            if h.startswith(target_prefix):
                return nonce
            nonce += 1
    return None


_POW_CHALLENGE_RE = re.compile(
    r"""(?:window\.pow_challenge|pow_challenge|["']challenge["'])\s*[=:]\s*["']([^"']{4,})["']""",
    re.IGNORECASE,
)


def extract_pow_challenge(
    text: str = "",
    headers: dict[str, str] | None = None,
    cookies: Any = None,
) -> str | None:
    """Extracts firewallPoW challenge token from headers, cookies, HTML body or JSON payload."""
    if cookies:
        if isinstance(cookies, dict):
            cand = cookies.get("pow_challenge") or cookies.get("challenge")
            if isinstance(cand, str) and cand:
                return cand
        elif isinstance(cookies, (list, tuple)):
            for c in cookies:
                if isinstance(c, dict) and c.get("name") in ("pow_challenge", "challenge") and c.get("value"):
                    return str(c["value"])

    if headers:
        set_cookie = headers.get("set-cookie") or headers.get("Set-Cookie") or ""
        if "pow_challenge=" in set_cookie:
            m = re.search(r"pow_challenge=([^;,\s]+)", set_cookie)
            if m:
                return m.group(1)

    if not text:
        return None

    stripped = text.strip()
    if (stripped.startswith("{") and stripped.endswith("}")) or (stripped.startswith("[") and stripped.endswith("]")):
        try:
            data = json.loads(stripped)
            if isinstance(data, dict):
                cand = data.get("challenge") or data.get("pow_challenge") or data.get("challenge_jwt")
                if isinstance(cand, str) and cand:
                    return cand
        except Exception:
            pass

    m = _POW_CHALLENGE_RE.search(text)
    if m:
        return m.group(1)
    m2 = re.search(r"""pow_challenge=([^;,\s"'&]{4,})""", text)
    if m2:
        return m2.group(1)
    return None


JS_GET_CHALLENGE = """
async (challengeArg) => {
    try {
        let challenge = challengeArg;
        if (!challenge && typeof document !== 'undefined' && document.cookie) {
            const m = document.cookie.match(/(?:^|; )pow_challenge=([^;]*)/);
            if (m) challenge = decodeURIComponent(m[1]);
        }
        if (!challenge && typeof window !== 'undefined') {
            challenge = window.pow_challenge || window.challenge || null;
            if (!challenge) {
                const m = document.documentElement.innerHTML.match(/["']challenge["']\\s*:\\s*["']([^"']+)["']/);
                if (m) challenge = m[1];
            }
        }
        if (challenge && typeof document !== 'undefined') {
            document.cookie = 'pow_challenge=' + encodeURIComponent(challenge) + '; Path=/; SameSite=Lax';
        }
        const bodyObj = challenge ? { challenge: challenge } : {};
        const resp = await fetch('/web/3/firewallPow/get', {
            method: 'POST',
            credentials: 'include',
            headers: {
                'Content-Type': 'application/json',
                'Accept': 'application/json',
                'X-Requested-With': 'XMLHttpRequest'
            },
            body: JSON.stringify(bodyObj)
        });
        if (!resp.ok) {
            return { success: false, status: resp.status, reason: 'get_status_' + resp.status };
        }
        const data = await resp.json();
        const jwt = (data && data.success && data.success.result && data.success.result.challenge_jwt) ||
                    data.challenge_jwt || data.challenge || (data.result && data.result.challenge) || data;
        let id = data.id;
        let compl = data.compl || 4;
        if (!id && typeof jwt === 'string' && jwt.includes('.')) {
            try {
                const parts = jwt.split('.');
                const payloadStr = atob(parts[1].replace(/-/g, '+').replace(/_/g, '/'));
                const payload = JSON.parse(payloadStr);
                id = payload.id;
                compl = payload.compl || compl;
            } catch (e) {}
        }
        return {
            success: true,
            id: id || '',
            compl: compl,
            jwt: typeof jwt === 'string' ? jwt : JSON.stringify(jwt)
        };
    } catch (err) {
        return { success: false, error: String(err) };
    }
}
"""

JS_VERIFY_CHALLENGE = """
async (args) => {
    try {
        const resp = await fetch('/web/3/firewallPow/verify', {
            method: 'POST',
            credentials: 'include',
            headers: {
                'Content-Type': 'application/json',
                'Accept': 'application/json',
                'X-Requested-With': 'XMLHttpRequest'
            },
            body: JSON.stringify({
                challenge: args.jwt,
                nonce: args.nonce
            })
        });
        if (!resp.ok) {
            return { verified: false, status: resp.status };
        }
        const data = await resp.json();
        const verified = Boolean(
            (data && data.success && data.success.result && data.success.result.verified) ||
            (data && data.verified) ||
            (data && data.result && data.result.verified)
        );
        if (verified && typeof document !== 'undefined') {
            document.cookie = 'pow_solved=1; Path=/; Max-Age=10; SameSite=Lax';
        }
        return { verified: verified, data: data };
    } catch (err) {
        return { verified: false, error: String(err) };
    }
}
"""


async def solve_firewall_pow(
    page: Any,
    html: str = "",
    challenge_token: str | None = None,
    headers: dict[str, str] | None = None,
    cookies: Any = None,
) -> bool:
    """Executes full firewallPoW solve flow in page context:
    1. Fetches challenge JWT via POST /web/3/firewallPow/get
    2. Computes SHA-256 Proof of Work nonce in Python (5-15ms)
    3. Submits verification via POST /web/3/firewallPow/verify
    Returns True if unblock was verified.
    """
    token = challenge_token or extract_pow_challenge(html, headers=headers, cookies=cookies)
    try:
        get_res = await page.evaluate(JS_GET_CHALLENGE, token)
        if not isinstance(get_res, dict) or not get_res.get("success"):
            logger.warning("firewallPoW get failed: {} (has_token={})", get_res, bool(token))
            return False

        challenge_id = get_res.get("id")
        compl = int(get_res.get("compl") or 4)
        jwt = get_res.get("jwt")
        if not challenge_id or not jwt:
            logger.warning("firewallPoW missing id or jwt: {}", get_res)
            return False

        # Fast CPU nonce search
        nonce = find_nonce(challenge_id, complexity=compl)
        if nonce is None:
            logger.warning("firewallPoW could not find nonce for challenge {}", challenge_id)
            return False

        logger.info("firewallPoW solved nonce={} for id={} (compl={}) in Python. Verifying...", nonce, challenge_id, compl)

        # Submit verification in page session
        verify_res = await page.evaluate(JS_VERIFY_CHALLENGE, {"jwt": jwt, "nonce": nonce})
        if isinstance(verify_res, dict) and verify_res.get("verified"):
            logger.info("firewallPoW verified successfully! Unblock TTL active.")
            return True
        else:
            logger.warning("firewallPoW verify rejected: {}", verify_res)
            return False
    except Exception as exc:
        logger.warning("Error solving firewallPoW in page: {}", exc)
        return False


def _normalize_cookies_for_http(cookies: Any) -> dict[str, str]:
    if not cookies:
        return {}
    if isinstance(cookies, dict):
        return {str(k): str(v) for k, v in cookies.items() if k and v is not None}
    if isinstance(cookies, (list, tuple)):
        return {str(c["name"]): str(c["value"]) for c in cookies if isinstance(c, dict) and c.get("name") and c.get("value") is not None}
    return {}


def _parse_pow_jwt(data: Any) -> tuple[str, int, str]:
    if not isinstance(data, dict):
        return "", 0, ""
    jwt_raw = (
        data.get("jwt") or data.get("challenge_jwt") or data.get("challenge")
        or (data.get("success", {}).get("result", {}).get("challenge_jwt") if isinstance(data.get("success"), dict) else None)
        or (data.get("result", {}).get("challenge") if isinstance(data.get("result"), dict) else None)
    )
    if not isinstance(jwt_raw, str) or not jwt_raw:
        return "", 0, ""
    cid, compl = data.get("id") or "", int(data.get("compl") or 4)
    if not cid and "." in jwt_raw:
        try:
            parts = jwt_raw.split(".")
            rem = len(parts[1]) % 4
            p = json.loads(base64.urlsafe_b64decode(parts[1] + ("=" * (4 - rem) if rem else "")))
            cid, compl = p.get("id", cid), int(p.get("compl", compl))
        except Exception:
            pass
    return cid, compl, jwt_raw


def _extract_response_cookies(resp: Any, jar: dict[str, str]) -> dict[str, str]:
    headers = getattr(resp, "headers", None) or {}
    sc_list = headers.get_list("set-cookie") if hasattr(headers, "get_list") else [
        v for k, v in getattr(headers, "items", lambda: [])() if str(k).lower() == "set-cookie"
    ]
    for sc in sc_list:
        first = sc.split(";")[0].strip()
        if "=" in first:
            name, val = first.split("=", 1)
            if name.strip().lower() not in ("path", "domain", "expires", "samesite", "httponly", "secure", "max-age"):
                jar[name.strip()] = val.strip()
    if hasattr(getattr(resp, "cookies", None), "items"):
        jar.update({str(k): str(v) for k, v in resp.cookies.items()})
    return jar


def _create_pow_http_client(
    endpoint: str | None,
    cookies: Any,
    timeout_sec: float,
    client: Any,
    headers: dict[str, str],
    proxy_credentials: Any = None,
    challenge_token: str | None = None,
) -> tuple[Any, bool, dict[str, Any]]:
    from hunter.transport.recovery_transport import apply_scoped_cookies_to_client, build_recovery_http_client
    if client is not None:
        apply_scoped_cookies_to_client(client, cookies)
        if challenge_token and hasattr(client, "cookies"):
            client.cookies.set("pow_challenge", challenge_token, domain=".avito.ru", path="/")
        kw: dict[str, Any] = {"headers": headers}
        if not hasattr(client, "cookies"):
            kw["cookies"] = _normalize_cookies_for_http(cookies)
        return client, False, kw
    c, owns_client = build_recovery_http_client(
        endpoint=endpoint, headers=headers, proxy_credentials=proxy_credentials, timeout_sec=timeout_sec
    )
    apply_scoped_cookies_to_client(c, cookies)
    if challenge_token and hasattr(c, "cookies"):
        c.cookies.set("pow_challenge", challenge_token, domain=".avito.ru", path="/")
    return c, owns_client, {"headers": headers}


async def _verify_pow_solution(
    client: Any, jwt_str: str, nonce: int, post_kwargs: dict[str, Any], cookie_jar: dict[str, str]
) -> bool:
    resp = await client.post(
        "https://www.avito.ru/web/3/firewallPow/verify",
        json={"challenge": jwt_str, "nonce": nonce},
        **post_kwargs,
    )
    if getattr(resp, "status_code", 0) != 200:
        return False
    _extract_response_cookies(resp, cookie_jar)
    vdata = resp.json() if callable(getattr(resp, "json", None)) else {}
    return bool(
        (isinstance(vdata.get("success"), dict) and vdata["success"].get("result", {}).get("verified"))
        or vdata.get("verified")
        or (isinstance(vdata.get("result"), dict) and vdata["result"].get("verified"))
    )


async def solve_firewall_pow_http(
    challenge_token: str,
    cookies: Any = None,
    headers: dict[str, str] | None = None,
    endpoint: str | None = None,
    proxy_credentials: Any = None,
    timeout_sec: float = 15.0,
    http_client: Any = None,
    executor: Any = None,
) -> tuple[bool, list[dict[str, Any]]]:
    """Solves firewallPoW challenge directly via HTTP without browser emulation."""
    cookie_jar = _normalize_cookies_for_http(cookies)
    if challenge_token and "pow_challenge" not in cookie_jar:
        cookie_jar["pow_challenge"] = challenge_token
    req_headers = {
        "Accept": "*/*", "Content-Type": "application/json",
        "Origin": "https://www.avito.ru", "Referer": "https://www.avito.ru/",
        "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "same-origin",
    }
    if headers:
        ua = next((v for k, v in headers.items() if k.lower() == "user-agent"), None)
        if ua:
            req_headers["User-Agent"] = ua

    client, owns_client, post_kw = _create_pow_http_client(
        endpoint, cookies, timeout_sec, http_client, req_headers,
        proxy_credentials=proxy_credentials, challenge_token=challenge_token,
    )
    try:
        get_res = await client.post(
            "https://www.avito.ru/web/3/firewallPow/get", json={"challenge": challenge_token}, **post_kw
        )
        if getattr(get_res, "status_code", 0) != 200:
            return False, []
        _extract_response_cookies(get_res, cookie_jar)
        data = get_res.json() if callable(getattr(get_res, "json", None)) else {}
        challenge_id, compl, jwt_str = _parse_pow_jwt(data)
        if not challenge_id or not jwt_str:
            return False, []
        from hunter.transport.pow_execution import run_cpu_bound_cooperative
        try:
            nonce = await run_cpu_bound_cooperative(find_nonce, challenge_id, compl, timeout_sec=timeout_sec, executor=executor)
        except Exception:
            return False, []
        if nonce is None or not await _verify_pow_solution(client, jwt_str, nonce, post_kw, cookie_jar):
            return False, []
        cookie_jar.pop('pow_challenge', None)  # purge_spent_challenge
        cookie_jar["pow_solved"] = "1"
        from hunter.transport.recovery_transport import update_cookie_list_from_jar
        solved_cookies = update_cookie_list_from_jar(cookies, cookie_jar)
        cookie_jar.pop('pow_solved', None)  # strip_ephemeral_cookies (Max-Age bounded)
        return True, solved_cookies
    except HttpDeferred:
        raise
    except Exception as exc:
        logger.warning("HTTP firewallPoW solver error: {}", exc)
        return False, []
    finally:
        if owns_client and hasattr(client, "aclose"):
            await client.aclose()

