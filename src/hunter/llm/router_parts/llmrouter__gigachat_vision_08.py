"""GigaChat vision helpers for LLMRouter."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from hunter.llm.router_parts.llmerror_01 import LLMError, extract_json_dict, sanitize_log_message
from hunter.llm.router_parts.llmrouter__gemini_04 import log_gemini_llm_call


async def upload_gigachat_vision_files(
    upload_fn: Any,
    image_paths: list[Path],
) -> list[str]:
    file_ids: list[str] = []
    for p in image_paths[:3]:
        if p.exists() and p.stat().st_size > 0:
            try:
                if file_ids:
                    await asyncio.sleep(0.3)
                fid = await upload_fn(p.read_bytes(), filename=p.name)
                if fid:
                    file_ids.append(fid)
            except Exception as u_exc:
                logger.debug("upload error for {}: {}", p.name, u_exc)
    return file_ids


async def log_gigachat_vision_call(
    model: str,
    call_type: str,
    product_id: str | None,
    avito_id: str | None,
    usage: dict,
    lat_ms: int,
) -> None:
    try:
        from hunter.core.db import get_db

        p_tok = usage.get("prompt_tokens", 0)
        c_tok = usage.get("completion_tokens", 0)
        tot_tok = usage.get("total_tokens", p_tok + c_tok)
        await get_db().log_llm_call(
            provider="gigachat",
            model=model,
            call_type=call_type,
            product_id=product_id,
            avito_id=avito_id,
            tokens_prompt=p_tok,
            tokens_completion=c_tok,
            tokens_total=tot_tok,
            latency_ms=lat_ms,
            status="ok",
        )
    except Exception:
        pass


async def execute_gigachat_vision(
    token: str,
    file_ids: list[str],
    prompt: str,
    *,
    system: str = "",
    call_type: str = "vision",
    product_id: str | None = None,
    avito_id: str | None = None,
) -> dict | None:
    url = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({
        "role": "user",
        "content": prompt,
        "attachments": file_ids,
    })
    for model in ["GigaChat-2-Max", "GigaChat-2-Pro", "GigaChat-Max"]:
        t0 = time.perf_counter()
        try:
            payload = {
                "model": model,
                "messages": messages,
                "temperature": 0.0,
            }
            async with httpx.AsyncClient(verify=False, timeout=60) as client:
                resp = await client.post(url, headers=headers, json=payload)
                lat_ms = int((time.perf_counter() - t0) * 1000)
                if resp.status_code == 200:
                    resp_json = resp.json()
                    content = resp_json["choices"][0]["message"]["content"]
                    res = extract_json_dict(content)
                    await log_gigachat_vision_call(
                        model, call_type, product_id, avito_id,
                        resp_json.get("usage") or {}, lat_ms,
                    )
                    if res:
                        logger.info("gigachat vision succeeded: model={}", model)
                        return res
                else:
                    logger.warning("gigachat {} vision HTTP {}: {}", model, resp.status_code, resp.text[:200])
        except Exception as m_exc:
            logger.warning("gigachat {} vision error: {}", model, m_exc)
    return None


async def execute_vision_dual(
    gemini_vision_fn: Any,
    gigachat_vision_fn: Any,
    settings: Any,
    image_paths: list[Path],
    prompt: str,
    *,
    system: str = "",
    resolution: str = "MEDIA_RESOLUTION_HIGH",
    call_type: str = "vision",
    product_id: str | None = None,
    avito_id: str | None = None,
) -> dict[str, Any]:
    res: dict[str, Any] = {"provider1": None, "provider2": None}
    if image_paths:
        for idx, key in enumerate(settings.gemini_keys, 1):
            try:
                p1 = await gemini_vision_fn(
                    image_paths, prompt, system=system, key=key,
                    resolution=resolution, call_type=call_type,
                    product_id=product_id, avito_id=avito_id,
                )
                if p1:
                    res["provider1"] = p1
                    break
            except Exception as exc:
                logger.warning("gemini vision acc{} failed in dual query: {}", idx, exc)

    if settings.gigachat_credentials and image_paths:
        try:
            p2 = await gigachat_vision_fn(
                image_paths, prompt, system=system, call_type=call_type,
                product_id=product_id, avito_id=avito_id,
            )
            if p2:
                res["provider2"] = p2
        except Exception as exc:
            logger.warning("gigachat vision failed in dual query: {}", exc)

    return res


async def handle_gemini_vision_completion(
    data: dict | None,
    last_error: Any,
    model: str,
    call_type: str,
    product_id: str | None,
    avito_id: str | None,
    lat_ms: int,
    record_result_fn: Any,
) -> dict:
    if data is None:
        record_result_fn(False)
        err_str = str(last_error)
        is_429 = "429" in err_str or "RESOURCE_EXHAUSTED" in err_str.upper() or "QUOTA" in err_str.upper()
        await log_gemini_llm_call(
            model, call_type, product_id, avito_id, lat_ms, "429" if is_429 else "error",
            error_msg=sanitize_log_message(err_str)[:200],
        )
        raise LLMError(f"gemini vision failed: {sanitize_log_message(str(last_error))}")

    record_result_fn(True)
    usage = data.get("usageMetadata") or {}
    p_tok, c_tok = usage.get("promptTokenCount", 0), usage.get("candidatesTokenCount", 0)
    tot_tok = usage.get("totalTokenCount", p_tok + c_tok)
    await log_gemini_llm_call(
        model, call_type, product_id, avito_id, lat_ms, "ok", tokens=(p_tok, c_tok, tot_tok),
    )
    candidates = data.get("candidates") or []
    if not candidates or not candidates[0].get("content"):
        raise LLMError(f"Gemini vision returned no content: {data}")
    parts = candidates[0]["content"].get("parts") or []
    if not parts or "text" not in parts[0]:
        raise LLMError(f"Gemini vision returned empty parts: {data}")
    return extract_json_dict(parts[0]["text"])


