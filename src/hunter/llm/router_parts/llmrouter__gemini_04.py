from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from hunter.core.config import get_settings
from hunter.llm.router_parts.llmerror_01 import (
    LLMError,
    LLMRouterBase,
    categorize_error,
    extract_json_dict,
    sanitize_log_message,
)


def build_gemini_payload(prompt: str, system: str) -> dict[str, Any]:
    contents = []
    if system:
        contents.append({"role": "user", "parts": [{"text": f"SYSTEM INSTRUCTION: {system}"}]})
        contents.append({
            "role": "model",
            "parts": [{"text": "Understood. I will strictly output a valid JSON object."}],
        })
    contents.append({"role": "user", "parts": [{"text": prompt}]})
    return {
        "contents": contents,
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
        },
    }


def extract_gemini_candidate_text(data: dict[str, Any]) -> str:
    candidates = data.get("candidates") or []
    if not candidates or not candidates[0].get("content"):
        raise LLMError(f"Gemini returned no content: {data}")
    parts = candidates[0]["content"].get("parts") or []
    if not parts or "text" not in parts[0]:
        raise LLMError(f"Gemini returned empty parts: {data}")
    return parts[0]["text"]


async def execute_gemini_call(prompt: str, *, system: str, key: str | None = None) -> dict[str, Any]:
    settings = get_settings()
    api_key = key or (settings.gemini_keys[0] if settings.gemini_keys else None)
    if not api_key:
        raise LLMError("no Gemini API key configured")
    model = LLMRouterBase.get_preferred_gemini_model() or settings.gemini_model or "gemini-3.5-flash"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    headers = {"x-goog-api-key": api_key}
    payload = build_gemini_payload(prompt, system)

    data = None
    last_error = None
    for attempt in range(2):
        proxy = settings.http_proxy or None
        try:
            async with httpx.AsyncClient(timeout=45, proxy=proxy) as client:
                resp = await client.post(url, headers=headers, json=payload)
                if resp.status_code in (429, 503) and model != "gemini-3.1-flash-lite":
                    logger.warning(
                        'gemini {} on {}, falling back to '
                        'gemini-3.1-flash-lite', resp.status_code, model)
                    LLMRouterBase.set_gemini_model_fallback(300.0)
                    model = "gemini-3.1-flash-lite"
                    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
                    await asyncio.sleep(1.0)
                    continue
                if resp.status_code == 429 and attempt == 0:
                    logger.warning("gemini 429 rate limit (cat=quota), retrying in 2s")
                    await asyncio.sleep(2.0)
                    continue
                if resp.status_code != 200:
                    cat = categorize_error(resp.status_code, resp.text)
                    sanitized_body = sanitize_log_message(resp.text[:300])
                    logger.warning(
                        'gemini non-200 [{}] (cat={}): '
                        '{}', resp.status_code, cat, sanitized_body)
                    resp.raise_for_status()
                data = resp.json()
                logger.info(
                    'gemini call succeeded, modelVersion: '
                    '{}', data.get("modelVersion", model))
                break
        except Exception as exc:
            last_error = exc
            sanitized_msg = sanitize_log_message(str(exc))
            logger.warning("gemini attempt {} failed: {}", attempt + 1, sanitized_msg)
            if attempt == 1:
                raise LLMError(sanitized_msg) from None

    if data is None:
        raise LLMError(f"gemini failed: {sanitize_log_message(str(last_error))}")

    text = extract_gemini_candidate_text(data)
    return extract_json_dict(text)


async def upload_gigachat_file_impl(
    token_getter: Any,
    content: bytes,
    filename: str = "image.jpg",
) -> str:
    url = "https://gigachat.devices.sberbank.ru/api/v1/files"
    files = {"file": (filename, content, "image/jpeg")}
    data = {"purpose": "general"}
    for attempt in range(3):
        token = await token_getter()
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(verify=False, timeout=30) as client:
            resp = await client.post(url, headers=headers, files=files, data=data)
            if resp.status_code == 429:
                await asyncio.sleep(1.0 * (attempt + 1))
                continue
            resp.raise_for_status()
            file_id = resp.json().get("id")
            if not file_id:
                raise LLMError("GigaChat file upload returned no id")
            return file_id
    raise LLMError("GigaChat file upload failed after retries")


def build_gemini_vision_payload(image_paths: list[Path], prompt: str, system: str, resolution: str) -> dict[str, Any]:
    text = f"SYSTEM INSTRUCTIONS:\n{system}\n\nUSER REQUEST:\n{prompt}" if system else prompt
    parts: list[dict] = [{"text": text}]
    mime_map = {".png": "image/png", ".webp": "image/webp"}
    for p in image_paths[:4]:
        if p.exists() and p.stat().st_size > 0:
            b64 = base64.b64encode(p.read_bytes()).decode("utf-8")
            part_dict: dict[str, Any] = {
                'inline'
                'Data': {"mimeType": mime_map.get(p.suffix.lower(), "image/jpeg"), "data": b64}}
            if resolution:
                part_dict["mediaResolution"] = {"level": resolution}
            parts.append(part_dict)

    gen_cfg: dict[str, Any] = {"temperature": 0.0, "responseMimeType": "application/json"}
    if resolution:
        gen_cfg["mediaResolution"] = resolution
    return {"contents": [{"parts": parts}], "generationConfig": gen_cfg}


async def log_gemini_llm_call(
    model: str, call_type: str, product_id: str | None, avito_id: str | None,
    lat_ms: int, status_val: str, error_msg: str | None = None,
    tokens: tuple[int, int, int] = (0, 0, 0),
) -> None:
    try:
        from hunter.core.db import get_db
        p_tok, c_tok, tot_tok = tokens
        await get_db().log_llm_call(
            provider="gemini", model=model, call_type=call_type,
            product_id=product_id, avito_id=avito_id, tokens_prompt=p_tok,
            tokens_completion=c_tok, tokens_total=tot_tok, latency_ms=lat_ms,
            status=status_val, error_msg=error_msg,
        )
    except Exception:
        pass


async def execute_gemini_vision_retry(
    payload: dict[str, Any], api_key: str, initial_model: str, proxy: str | None,
) -> tuple[dict[str, Any] | None, Exception | None, str]:
    delays_503 = [3.0, 6.0, 12.0]
    attempt_503, data, last_error, attempt = 0, None, None, 0
    model = initial_model
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    headers = {"x-goog-api-key": api_key}

    while attempt < 4:
        attempt += 1
        try:
            async with httpx.AsyncClient(timeout=45, proxy=proxy) as client:
                resp = await client.post(url, headers=headers, json=payload)
                if resp.status_code in (429, 503) and model != "gemini-3.1-flash-lite":
                    logger.warning(
                        'gemini vision {} on {}, falling back to '
                        'gemini-3.1-flash-lite', resp.status_code, model)
                    LLMRouterBase.set_gemini_model_fallback(300.0)
                    model = "gemini-3.1-flash-lite"
                    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
                    await asyncio.sleep(1.0)
                    continue
                if resp.status_code == 429 and attempt == 1:
                    logger.warning("gemini vision 429 rate limit (cat=quota), retrying in 2s")
                    await asyncio.sleep(2.0)
                    continue
                if resp.status_code == 503 and attempt_503 < len(delays_503):
                    delay = delays_503[attempt_503]
                    attempt_503 += 1
                    logger.warning("gemini vision 503 Service Unavailable (attempt {}/3), retrying in {}s", attempt_503, delay)
                    await asyncio.sleep(delay)
                    continue
                if resp.status_code != 200:
                    cat = categorize_error(resp.status_code, resp.text)
                    sanitized_body = sanitize_log_message(resp.text[:300])
                    logger.warning(
                        'gemini vision non-200 [{}] (cat={}): '
                        '{}', resp.status_code, cat, sanitized_body)
                    resp.raise_for_status()
                data = resp.json()
                logger.info("gemini vision succeeded, modelVersion: {}", data.get("modelVersion", model))
                break
        except Exception as exc:
            last_error = exc
            sanitized_msg = sanitize_log_message(str(exc))
            logger.warning("gemini vision attempt {} failed: {}", attempt, sanitized_msg)
            if ("503" in str(exc) or "Service Unavailable" in str(exc)) and attempt_503 < len(delays_503):
                delay = delays_503[attempt_503]
                attempt_503 += 1
                logger.warning("gemini vision 503 error (attempt {}/3), retrying in {}s: {}", attempt_503, delay, sanitized_msg)
                await asyncio.sleep(delay)
                continue
            if attempt >= 2 and attempt_503 == 0:
                break
    return data, last_error, model


async def execute_openrouter_vision(
    settings: Any,
    image_paths: list[Path],
    prompt: str,
    system: str = "",
) -> dict | None:
    b64_parts = []
    for p in image_paths[:3]:
        if p.exists() and p.stat().st_size > 0:
            b64 = base64.b64encode(p.read_bytes()).decode("utf-8")
            b64_parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
            })
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.openrouter_api_key.get_secret_value()}",
        "Content-Type": "application/json",
    }
    user_content: list[dict] = [{"type": "text", "text": prompt}, *b64_parts]
    or_messages: list[dict] = []
    if system:
        or_messages.append({"role": "system", "content": system})
    or_messages.append({"role": "user", "content": user_content})
    or_payload = {
        "model": settings.openrouter_model or "google/gemini-2.0-flash-exp:free",
        "messages": or_messages,
        "temperature": 0.0,
        "response_format": {"type": "json_object"},
    }
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(url, headers=headers, json=or_payload)
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        res = extract_json_dict(content)
        return res if res else None


async def execute_ollama_vision(
    settings: Any,
    image_paths: list[Path],
    prompt: str,
    system: str = "",
) -> dict | None:
    b64_images = []
    for p in image_paths[:3]:
        if p.exists() and p.stat().st_size > 0:
            b64_images.append(base64.b64encode(p.read_bytes()).decode("utf-8"))
    if not b64_images:
        return None
    ol_payload = {
        "model": settings.ollama_vlm_model,
        "prompt": f"{system}\n\n{prompt}" if system else prompt,
        "images": b64_images,
        "stream": False,
        "format": "json",
    }
    async with httpx.AsyncClient(timeout=45) as client:
        resp = await client.post(f"{settings.ollama_base_url}/api/generate", json=ol_payload)
        resp.raise_for_status()
        res = extract_json_dict(resp.json().get("response") or "{}")
        return res if res else None
