"""Gemini vision execution helpers for LLMRouter."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from loguru import logger

from hunter.llm.router_parts.llmerror_01 import LLMError
from hunter.llm.router_parts.llmrouter__gemini_04 import (
    build_gemini_vision_payload,
    execute_gemini_vision_retry,
)
from hunter.llm.router_parts.llmrouter__gigachat_vision_08 import (
    handle_gemini_vision_completion,
)


def resolve_gemini_vision_key_and_model(
    settings: Any,
    key: str | None,
    preferred_model: str | None,
    model: str | None,
) -> tuple[str, str]:
    api_key = key or (settings.gemini_keys[0] if settings.gemini_keys else None)
    if not api_key:
        raise LLMError("no Gemini API key configured")
    chosen_model = model or preferred_model or settings.gemini_model or "gemini-3.5-flash"
    return api_key, chosen_model


def check_gemini_vision_circuit(is_open: bool, circuit_until: float) -> None:
    if is_open:
        wait_left = int(circuit_until - time.time())
        logger.warning("Gemini vision circuit open ({}s left), bypassing Gemini", max(0, wait_left))
        raise LLMError(f"Gemini vision circuit open ({wait_left}s left)")


async def run_timed_gemini_vision_retry(
    payload: dict,
    api_key: str,
    chosen_model: str,
    http_proxy: str | None,
) -> tuple[dict | None, Any, str, int]:
    t0 = time.perf_counter()
    data, last_error, final_model = await execute_gemini_vision_retry(
        payload, api_key, chosen_model, http_proxy,
    )
    lat_ms = int((time.perf_counter() - t0) * 1000)
    return data, last_error, final_model, lat_ms


async def execute_gemini_vision_flow(
    settings: Any,
    image_paths: list[Path],
    prompt: str,
    *,
    system: str = "",
    key: str | None = None,
    resolution: str = "MEDIA_RESOLUTION_HIGH",
    call_type: str = "vision",
    product_id: str | None = None,
    avito_id: str | None = None,
    model: str | None = None,
    preferred_model: str | None = None,
    is_circuit_open: bool = False,
    circuit_until: float = 0.0,
    record_result_fn: Any = None,
) -> dict:
    api_key, chosen_model = resolve_gemini_vision_key_and_model(
        settings, key, preferred_model, model,
    )
    check_gemini_vision_circuit(is_circuit_open, circuit_until)

    payload = build_gemini_vision_payload(image_paths, prompt, system, resolution)
    data, last_error, final_model, lat_ms = await run_timed_gemini_vision_retry(
        payload, api_key, chosen_model, settings.http_proxy or None,
    )
    return await handle_gemini_vision_completion(
        data, last_error, final_model, call_type, product_id, avito_id, lat_ms,
        record_result_fn,
    )
