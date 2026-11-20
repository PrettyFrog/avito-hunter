"""Offline GeeTest v4 solver and Avito verification adapter (S5 / T-483)."""

from __future__ import annotations

import asyncio
import binascii
import hashlib
import io
import json
import random
import secrets
from typing import Any
import httpx
from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey.RSA import construct
from Crypto.Util.Padding import pad
from PIL import Image, ImageFilter
import numpy as np
import scipy.signal
from hunter.transport.metered_http import HttpDeferred

CAPTCHA_SOLVE_DELAY_SEC: float = 1.35
GEETEST_BASE_URL = "https://gcaptcha4.geevisit.com"
AVITO_CAPTCHA_VERIFY_URL = "https://www.avito.ru/web/3/firewallCaptcha/verify"
AVITO_GEETEST_CAPTCHA_ID = "2d9c743cf7d63dbc9db578a608196bcd"
RSA_PUBKEY_HEX = (
    "00C1E3934D1614465B33053E7F48EE4EC87B14B95EF88947713D25EECBFF7E74C7977D02DC1D9451"
    "F79DD5D1C10C29ACB6A9B4D6FB7D0A0279B6719E1772565F09AF627715919221AEF91899CAE08C0D"
    "686D748B20A3603BE2318CA6BC2B59706592A9219D0BF05C9F65023A21D2330807252AE0066D59CE"
    "EFA5F2748EA80BAB81"
)
_RSA_KEY = construct((int(RSA_PUBKEY_HEX.lower(), 16), int("10001", 16)))


def find_puzzle_piece_position(slice_bytes: bytes, bg_bytes: bytes) -> float:
    try:
        import cv2
        bg = cv2.imdecode(np.frombuffer(bg_bytes, np.uint8), cv2.IMREAD_COLOR)
        piece = cv2.imdecode(np.frombuffer(slice_bytes, np.uint8), cv2.IMREAD_COLOR)
        if bg is None or piece is None:
            raise ValueError("Failed to decode image bytes")
        piece_pad = cv2.copyMakeBorder(piece, 1, 1, 1, 1, cv2.BORDER_CONSTANT, value=0)
        edge_piece = cv2.Canny(piece_pad, 100, 200)[1:-1, 1:-1]
        edge_bg = cv2.Canny(bg, 100, 200)
        res = cv2.matchTemplate(edge_bg, edge_piece, cv2.TM_CCOEFF_NORMED)
        _, _, _, max_loc = cv2.minMaxLoc(res)
        return float(max_loc[0] + edge_piece.shape[1] // 2 - 41)
    except Exception:
        bg_img = Image.open(io.BytesIO(bg_bytes)).convert("L")
        slice_img = Image.open(io.BytesIO(slice_bytes)).convert("L")
        edge_bg = np.array(bg_img.filter(ImageFilter.FIND_EDGES), dtype=np.float32)
        edge_slice = np.array(slice_img.filter(ImageFilter.FIND_EDGES), dtype=np.float32)
        corr = scipy.signal.fftconvolve(edge_bg, edge_slice[::-1, ::-1], mode="valid")
        _, max_x = np.unravel_index(np.argmax(corr), corr.shape)
        return float(max_x + slice_img.width // 2 - 41)


def solve_gobang(board: list[list[int]]) -> list[list[int]] | None:
    n = len(board)
    lines: list[list[tuple[int, int]]] = []
    for r in range(n): lines.append([(r, c) for c in range(n)])
    for c in range(n): lines.append([(r, c) for r in range(n)])
    for sr in range(n): lines.append([(sr + i, i) for i in range(n - sr)])
    for sc in range(1, n): lines.append([(i, sc + i) for i in range(n - sc)])
    for sr in range(n): lines.append([(sr - i, i) for i in range(sr + 1)])
    for sc in range(1, n): lines.append([(n - 1 - i, sc + i) for i in range(n - sc)])
    for line in lines:
        elems = [board[r][c] for r, c in line]
        freq = {x: elems.count(x) for x in set(elems)}
        if n - 1 not in freq.values() or freq.get(0) == n - 1 or 0 not in elems:
            continue
        corr = next(num for num, cnt in freq.items() if cnt == n - 1 and num != 0)
        zero_pos = line[elems.index(0)]
        ex = set(line)
        for r in range(n):
            for c in range(n):
                if (r, c) not in ex and board[r][c] == corr:
                    return [[r, c], [zero_pos[0], zero_pos[1]]]
    return None


def _rand_hex(chars: int = 16) -> str:
    return secrets.token_hex(chars // 2)


def solve_geetest_pow(
    pow_detail: dict[str, Any], lot_number: str, captcha_id: str, stop_event: Any = None,
) -> dict[str, str]:
    bits = pow_detail.get("bits", 0)
    func_name = pow_detail.get("hashfunc", "md5")
    ver = pow_detail.get("version", "basic")
    dt = pow_detail.get("datetime", "")
    prefix = "0" * (bits // 4)
    base_s = f"{ver}|{bits}|{func_name}|{dt}|{captcha_id}|{lot_number}||"
    for i in range(500000):
        if i % 5000 == 0 and stop_event is not None and stop_event.is_set():
            return {"pow_msg": base_s, "pow_sign": ""}
        h = _rand_hex(16)
        cand = (base_s + h).encode("utf-8")
        h_val = hashlib.md5(cand).hexdigest() if func_name == "md5" else hashlib.sha256(cand).hexdigest()
        if h_val.startswith(prefix):
            return {"pow_msg": base_s + h, "pow_sign": h_val}
    return {"pow_msg": base_s, "pow_sign": ""}


def generate_w(
    data: dict[str, Any], captcha_id: str, set_left: float | None = None, ques: Any = None, stop_event: Any = None,
) -> str:
    lot = data.get("lot_number", "")
    pow_dict = solve_geetest_pow(data.get("pow_detail", {}), lot, captcha_id, stop_event=stop_event)
    passtime = int(CAPTCHA_SOLVE_DELAY_SEC * 1000) + random.randint(-30, 40)
    biht_val = str(int(time.time() * 1000) % 2000000000)
    base: dict[str, Any] = {
        "YYhg": "BjI0", **pow_dict,
        lot[1:5]: lot[24:28] if len(lot) >= 28 else "",
        "biht": biht_val, "device_id": "",
        "em": {"cp": 0, "ek": "11", "nt": 0, "ph": 0, "sc": 0, "si": 0, "wd": 1},
        "gee_guard": {"roe": {k: "3" for k in ("auh", "aup", "cdc", "egp", "res", "rew", "sep", "snh")}},
        "ep": "123", "geetest": "captcha", "lang": "rus", "lot_number": lot, "passtime": passtime,
    }
    if set_left is not None:
        base.update({"setLeft": set_left, "userresponse": set_left / 1.0059466666666665 + 2})
    elif ques is not None:
        base["userresponse"] = solve_gobang(ques)
    rand_key = _rand_hex(16).encode("utf-8")
    aes_enc = AES.new(rand_key, AES.MODE_CBC, b"0000000000000000").encrypt(pad(json.dumps(base).encode("utf-8"), 16))
    rsa_enc = PKCS1_v1_5.new(_RSA_KEY).encrypt(rand_key)
    return binascii.hexlify(aes_enc).decode() + binascii.hexlify(rsa_enc).decode()


def generate_x_cube_timing() -> str:
    """Simulates elapsed time in ms of 50 gl.readPixels calls in desktop browser WebGL benchmark."""
    base_ms = random.uniform(142.0, 228.0)
    sub_ms = random.choice([0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9])
    tail = random.uniform(1e-6, 9.9e-6)
    val = round(base_ms + sub_ms + tail, 6)
    formatted = f"{val:.6f}".rstrip("0")
    return f"{val:.1f}" if formatted.endswith(".") else formatted


async def submit_avito_captcha_verify(client: httpx.AsyncClient, seccode: dict[str, str], referer: str) -> bool:
    payload = {"captcha": "", "hCaptchaResponse": "", **seccode}
    cube = generate_x_cube_timing()
    hdrs = {
        "Referer": referer, "X-Cube": cube, "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
    }
    resp = await client.post(AVITO_CAPTCHA_VERIFY_URL, json=payload, headers=hdrs, timeout=15.0)
    if resp.status_code != 200:
        return False
    try:
        body = resp.json()
        return bool(body.get("success", {}).get("result", {}).get("verified"))
    except Exception:
        return False


import re
import time


def _parse_jsonp(text: str) -> dict[str, Any]:
    m = re.search(r"^[^(]*\((.*)\)[^)]*$", text.strip(), re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    try:
        return json.loads(text)
    except Exception:
        return {}


async def _get_firewall_captcha_id(c: httpx.AsyncClient, html: str) -> str | None:
    from hunter.transport.challenge_dispatch import CaptchaProviderType, resolve_firewall_captcha_dispatch
    disp = await resolve_firewall_captcha_dispatch(c, html)
    return disp.captcha_id if disp.is_supported and disp.provider_type == CaptchaProviderType.GEETEST else None


def _is_metered_client(client: Any) -> bool:
    from hunter.transport.metered_http import MeteredTransport
    if getattr(client, "gateway", None) is not None:
        return True
    return isinstance(getattr(client, "_transport", None), MeteredTransport)


def _init_geetest_client(
    client: httpx.AsyncClient | None,
    proxy_url: str | None,
    headers: dict[str, str] | None,
    cookies: Any,
    endpoint: str | None = None,
) -> tuple[httpx.AsyncClient, bool]:
    from hunter.transport.recovery_transport import apply_scoped_cookies_to_client, build_recovery_http_client
    c, owns_client = build_recovery_http_client(endpoint=proxy_url or endpoint, headers=headers, client=client)
    apply_scoped_cookies_to_client(c, cookies)
    return c, owns_client


async def _solve_geetest_w(c: httpx.AsyncClient, data: dict[str, Any], captcha_id: str, executor: Any = None) -> str:
    from hunter.transport.pow_execution import run_cpu_bound_cooperative
    ctype = data.get("captcha_type")
    gt_hdrs = {"Referer": "https://www.avito.ru/", "Connection": "close"}
    if ctype == "slide":
        slice_task = c.get(f"https://static.geetest.com/{data['slice']}", headers=gt_hdrs, timeout=15.0)
        bg_task = c.get(f"https://static.geetest.com/{data['bg']}", headers=gt_hdrs, timeout=15.0)
        slice_res, bg_res = await asyncio.gather(slice_task, bg_task)
        exact_pos = await run_cpu_bound_cooperative(find_puzzle_piece_position, slice_res.content, bg_res.content, executor=executor)
        human_jitter = round(random.uniform(-1.5, 1.5), 2)
        await asyncio.sleep(CAPTCHA_SOLVE_DELAY_SEC)
        return await run_cpu_bound_cooperative(generate_w, data, captcha_id, set_left=exact_pos + human_jitter, executor=executor)
    await asyncio.sleep(CAPTCHA_SOLVE_DELAY_SEC)
    if ctype in ("winlinze", "gobang"):
        return await run_cpu_bound_cooperative(generate_w, data, captcha_id, ques=data.get("ques", []), executor=executor)
    return await run_cpu_bound_cooperative(generate_w, data, captcha_id, executor=executor)


async def _execute_geetest_attempt(
    c: httpx.AsyncClient, gc: httpx.AsyncClient, captcha_id: str, executor: Any = None,
) -> tuple[bool, list[dict[str, Any]]]:
    ua = (c.headers.get("User-Agent") if hasattr(c, "headers") else None) or "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    gt_hdrs = {"Referer": "https://www.avito.ru/", "User-Agent": ua, "Accept": "*/*", "Connection": "close"}
    for attempt in range(2):
        try:
            cb = f"geetest_{int(time.time() * 1000)}"
            load_resp = await gc.get(
                f"{GEETEST_BASE_URL}/load",
                params={"captcha_id": captcha_id, "challenge": str(secrets.token_hex(16)), "client_type": "web", "lang": "rus", "callback": cb},
                headers=gt_hdrs, timeout=15.0,
            )
            data = _parse_jsonp(load_resp.text).get("data", {})
            if not data or not data.get("lot_number"):
                continue
            w = await _solve_geetest_w(gc, data, captcha_id, executor=executor)
            ver_resp = await gc.get(
                f"{GEETEST_BASE_URL}/verify",
                params={
                    "callback": f"geetest_{int(time.time() * 1000)}", "captcha_id": captcha_id,
                    "client_type": "web", "lot_number": data["lot_number"],
                    "payload": data.get("payload", ""), "process_token": data.get("process_token", ""),
                    "payload_protocol": "1", "pt": "1", "w": w,
                },
                headers=gt_hdrs, timeout=15.0,
            )
            seccode = _parse_jsonp(ver_resp.text).get("data", {}).get("seccode")
            if not seccode or not isinstance(seccode, dict):
                continue
            verified = await submit_avito_captcha_verify(c, seccode, referer="https://www.avito.ru/")
            if verified:
                from hunter.transport.recovery_transport import extract_scoped_cookies
                return True, extract_scoped_cookies(c, allowed_domain_suffix="avito.ru")
        except Exception as exc:
            from loguru import logger
            logger.warning("GeeTest attempt {} failed: {}: {}", attempt + 1, type(exc).__name__, exc)
            continue
    return False, []


async def solve_geetest_v4_flow(
    endpoint: str = "direct",
    headers: dict[str, str] | None = None,
    cookies: Any = None,
    html: str = "",
    proxy_url: str | None = None,
    client: httpx.AsyncClient | None = None,
    executor: Any = None,
    fast_path: bool = False,
) -> tuple[bool, list[dict[str, Any]]]:
    init_res = _init_geetest_client(client, proxy_url, headers, cookies, endpoint=endpoint)
    c, close_client = init_res if isinstance(init_res, tuple) else (init_res, client is None)
    gc, owns_gc = c, False
    if _is_metered_client(c):
        from hunter.transport.recovery_transport import format_proxy_url
        gc_headers = {"User-Agent": (headers or {}).get("User-Agent", "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")}
        gc, owns_gc = httpx.AsyncClient(proxy=format_proxy_url(endpoint or proxy_url), headers=gc_headers, timeout=15.0), True
    try:
        if fast_path:
            solved, new_cookies = await _execute_geetest_attempt(c, gc, AVITO_GEETEST_CAPTCHA_ID, executor=executor)
            if solved:
                return True, new_cookies
        captcha_id = await _get_firewall_captcha_id(c, html)
        if not captcha_id:
            return False, []
        return await _execute_geetest_attempt(c, gc, captcha_id, executor=executor)
    except HttpDeferred:
        raise
    except Exception as exc:
        from loguru import logger
        logger.warning("GeeTest v4 flow failed: {}: {}", type(exc).__name__, exc)
        return False, []
    finally:
        if owns_gc:
            await gc.aclose()
        if close_client:
            await c.aclose()
