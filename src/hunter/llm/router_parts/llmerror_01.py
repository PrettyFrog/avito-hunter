from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from typing import Any

import httpx
from loguru import logger

from hunter.core.config import get_settings


class LLMError(Exception):
    pass


def sanitize_log_message(msg: str) -> str:
    msg = re.sub(r"key=[^&\s'\"]+", "key=[REDACTED]", msg)
    msg = re.sub(r"Bearer\s+[^'\"]+", "Bearer [REDACTED]", msg)
    return msg


def categorize_error(status_code: int, msg: str) -> str:
    m = msg.lower()
    if status_code in (403, 451) or any(x in m for x in ("geo",
        'locatio'
        'n', "country", "region", "not supported in your country", "location is not supported")):
        return "geo"
    if status_code == 404 or "not found" in m:
        return "not_found"
    if status_code == 429 or any(x in m for x in ("quota", "resource_exhausted", "rate limit")):
        return "quota"
    if status_code == 400 or any(x in m for x in ("invalid", "bad request", "argument")):
        return "invalid"
    return "other"


def extract_json_dict(text: str) -> dict:
    if not text:
        return {}
    text = text.strip()
    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if match:
        raw = match.group(1).strip()
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            raw = text[start : end + 1]
        else:
            raw = text
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        logger.warning("failed to decode JSON from LLM: {}", text[:120])
        return {}


class LLMRouterBase:
    _gigachat_token: str | None = None
    _gigachat_token_expires: float = 0
    _token_lock: asyncio.Lock | None = None
    _gemini_vision_history: list[tuple[float, bool]] = []
    _gemini_vision_circuit_until: float = 0.0
    _active_gemini_model: str | None = "gemini-3.5-flash"
    _gemini_fallback_until: float = 0.0

    @classmethod
    def get_preferred_gemini_model(cls) -> str:
        now = time.time()
        if cls._gemini_fallback_until > now:
            return "gemini-3.1-flash-lite"
        return "gemini-3.5-flash"

    @classmethod
    def set_gemini_model_fallback(cls, duration_sec: float = 300.0) -> None:
        cls._gemini_fallback_until = time.time() + duration_sec
        cls._active_gemini_model = "gemini-3.1-flash-lite"

    @classmethod
    def _is_gemini_circuit_open(cls) -> bool:
        now = time.time()
        return cls._gemini_vision_circuit_until > now

    @classmethod
    def _record_gemini_vision_result(cls, success: bool) -> None:
        now = time.time()
        cls._gemini_vision_history = [
            (ts, err) for ts, err in cls._gemini_vision_history if now - ts <= 3600
        ]
        cls._gemini_vision_history.append((now, not success))
        if not success and len(cls._gemini_vision_history) >= 4:
            err_count = sum(1 for _, err in cls._gemini_vision_history if err)
            err_rate = err_count / len(cls._gemini_vision_history)
            if err_rate > 0.5:
                cls._gemini_vision_circuit_until = now + 1800  # 30 min bypass
                logger.warning(
                    'Gemini vision circuit breaker tripped (>50% errors in 1h: {}/{}={:.1%}), '
                        'bypassing for 30m',
                    err_count,
                    len(cls._gemini_vision_history),
                    err_rate,
                )

    @classmethod
    def _get_lock(cls) -> asyncio.Lock:
        if cls._token_lock is None:
            cls._token_lock = asyncio.Lock()
        return cls._token_lock

    async def complete_json(self, prompt: str, *, system: str = "") -> dict:
        settings = get_settings()
        errors: list[str] = []

        for idx, key in enumerate(settings.gemini_keys, 1):
            try:
                return await self._gemini(prompt, system=system, key=key)
            except Exception as exc:
                errors.append(f"gemini_acc{idx}: {exc}")
                logger.warning("gemini account {} failed: {}", idx, exc)

        if settings.gigachat_credentials:
            try:
                return await self._gigachat(prompt, system=system)
            except Exception as exc:
                errors.append(f"gigachat: {exc}")
                logger.warning("gigachat failed: {}", exc)

        if getattr(settings, "ollama_enabled", False):
            try:
                return await self._ollama(prompt, system=system)
            except Exception as exc:
                errors.append(f"ollama: {exc}")
                logger.debug("ollama unavailable: {}", exc)

        if getattr(settings, "openrouter_enabled", False) and settings.openrouter_api_key:
            try:
                return await self._openrouter(prompt, system=system)
            except Exception as exc:
                errors.append(f"openrouter: {exc}")
                logger.warning("openrouter failed: {}", exc)

        raise LLMError("; ".join(errors) or "no LLM configured or all providers failed")

    async def _get_gigachat_token(self) -> str:
        settings = get_settings()
        now = time.time()
        if self._gigachat_token and self._gigachat_token_expires > now + 60:
            return self._gigachat_token

        async with self._get_lock():
            now = time.time()
            if self._gigachat_token and self._gigachat_token_expires > now + 60:
                return self._gigachat_token

            creds = (
                settings.gigachat_credentials.get_secret_value().strip()
                if settings.gigachat_credentials
                else ""
            )
            if not creds:
                raise LLMError("gigachat credentials not set")

            url = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
            headers = {
                "Authorization": f"Basic {creds}",
                "RqUID": str(uuid.uuid4()),
                "Content-Type": "application/x-www-form-urlencoded",
            }
            data = {"scope": settings.gigachat_scope or "GIGACHAT_API_PERS"}

            async with httpx.AsyncClient(verify=False, timeout=30) as client:
                resp = await client.post(url, headers=headers, data=data)
                resp.raise_for_status()
                payload = resp.json()

            token = payload.get("access_token")
            if not token:
                raise LLMError("no access_token in gigachat auth response")

            expires_at = payload.get("expires_at", 0)
            self._gigachat_token = token
            self._gigachat_token_expires = (
                expires_at / 1000 if expires_at > 1e11 else now + 1800
            )
            return self._gigachat_token

    async def _gigachat(self, prompt: str, *, system: str) -> dict:
        token = await self._get_gigachat_token()
        url = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        messages = []
        full_system = (
            (system or "")
            + "\nВАЖНО: Отвечай ТОЛЬКО валидным JSON объектом {...}. "
            "Никакого вводного текста или markdown-пояснений вне JSON блока."
        ).strip()
        messages.append({"role": "system", "content": full_system})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": "GigaChat",
            "messages": messages,
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }

        async with httpx.AsyncClient(verify=False, timeout=60) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()

        content = data["choices"][0]["message"]["content"]
        return extract_json_dict(content)

    async def _ollama(self, prompt: str, *, system: str) -> dict:
        settings = get_settings()
        payload = {
            "model": settings.ollama_text_model,
            "prompt": f"{system}\n\n{prompt}".strip(),
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.1},
        }
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(f"{settings.ollama_base_url}/api/generate", json=payload)
            response.raise_for_status()
            data = response.json()
        return extract_json_dict(data.get("response") or "{}")

    async def _openrouter(self, prompt: str, *, system: str) -> dict:
        settings = get_settings()
        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {settings.openrouter_api_key.get_secret_value()}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/avito-hunter",
            "X-Title": "avito-hunter",
        }
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": settings.openrouter_model or "google/gemini-2.0-flash-exp:free",
            "messages": messages,
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()

        content = data["choices"][0]["message"]["content"]
        return extract_json_dict(content)
