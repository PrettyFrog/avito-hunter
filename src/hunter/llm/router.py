"""LLM providers. Round 1 never imports this on the hot path."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger

from hunter.core.config import get_settings
from hunter.llm.router_parts.llmerror_01 import (
    LLMError,
    LLMRouterBase,
    extract_json_dict,
)
from hunter.llm.router_parts.llmrouter__gemini_04 import (
    execute_gemini_call,
    execute_ollama_vision,
    execute_openrouter_vision,
    upload_gigachat_file_impl,
)
from hunter.llm.router_parts.llmrouter__gemini_vision_12 import (
    execute_gemini_vision_flow,
)
from hunter.llm.router_parts.llmrouter__gigachat_vision_08 import (
    execute_gigachat_vision,
    execute_vision_dual,
    upload_gigachat_vision_files,
)

__all__ = [
    "LLMError",
    "LLMRouter",
    "extract_json_dict",
]


class LLMRouter(LLMRouterBase):
    @classmethod
    def is_owner_authorized(cls, user_role: str | None, *, hidden_flag: bool = False) -> bool:
        return user_role == "owner" and bool(hidden_flag) is True

    async def _gemini(self, prompt: str, *, system: str, key: str | None = None) -> dict:
        return await execute_gemini_call(prompt, system=system, key=key)

    async def upload_gigachat_file(self, content: bytes, filename: str = "image.jpg") -> str:
        return await upload_gigachat_file_impl(self._get_gigachat_token, content, filename)

    async def complete_vision_json(
        self, image_paths: list[Path], prompt: str, *,
        system: str = "", resolution: str = "MEDIA_RESOLUTION_HIGH",
        call_type: str = "vision", product_id: str | None = None, avito_id: str | None = None,
    ) -> dict:
        settings = get_settings()
        errors: list[str] = []

        # 1. Native Google Gemini Vision (Primary Pro account -> Secondary account)
        if image_paths:
            for idx, key in enumerate(settings.gemini_keys, 1):
                try:
                    return await self._gemini_vision(
                        image_paths, prompt, system=system, key=key,
                        resolution=resolution, call_type=call_type,
                        product_id=product_id, avito_id=avito_id,
                    )
                except Exception as exc:
                    errors.append(f"gemini_vision_acc{idx}: {exc}")
                    logger.warning("gemini vision account {} failed: {}", idx, exc)

        # 2. GigaChat Vision (GigaChat-2-Max / GigaChat-2-Pro / GigaChat-Max)
        if settings.gigachat_credentials and image_paths:
            try:
                g_res = await self._gigachat_vision(
                    image_paths, prompt, system=system, call_type=call_type,
                    product_id=product_id, avito_id=avito_id,
                )
                if g_res:
                    return g_res
            except Exception as exc:
                errors.append(f"gigachat-vision: {exc}")
                logger.warning("gigachat vision failed: {}", exc)

        # 3. OpenRouter Multimodal (Gemini 2.0 Flash)
        if getattr(settings, "openrouter_enabled", False) and settings.openrouter_api_key and image_paths:
            try:
                res = await execute_openrouter_vision(settings, image_paths, prompt, system=system)
                if res:
                    return res
            except Exception as exc:
                errors.append(f"openrouter-vision: {exc}")
                logger.warning("openrouter vision failed: {}", exc)

        # 4. Local Ollama VLM
        if getattr(settings, "ollama_enabled", False) and image_paths:
            try:
                res = await execute_ollama_vision(settings, image_paths, prompt, system=system)
                if res:
                    return res
            except Exception as exc:
                errors.append(f"ollama-vision: {exc}")
                logger.warning("ollama vision failed: {}", exc)

        raise LLMError("; ".join(errors) or "no vision provider available")

    async def _gigachat_vision(
        self,
        image_paths: list[Path],
        prompt: str,
        *,
        system: str = "",
        call_type: str = "vision",
        product_id: str | None = None,
        avito_id: str | None = None,
    ) -> dict | None:
        settings = get_settings()
        if not (settings.gigachat_credentials and image_paths):
            return None
        token = await self._get_gigachat_token()
        file_ids = await upload_gigachat_vision_files(self.upload_gigachat_file, image_paths)
        if not file_ids:
            return None
        return await execute_gigachat_vision(
            token,
            file_ids,
            prompt,
            system=system,
            call_type=call_type,
            product_id=product_id,
            avito_id=avito_id,
        )

    async def complete_vision_dual(
        self,
        image_paths: list[Path],
        prompt: str,
        *,
        system: str = "",
        resolution: str = "MEDIA_RESOLUTION_HIGH",
        call_type: str = "vision",
        product_id: str | None = None,
        avito_id: str | None = None,
    ) -> dict[str, Any]:
        """Query primary provider (Gemini) and secondary provider (GigaChat) for vision triage."""
        return await execute_vision_dual(
            self._gemini_vision,
            self._gigachat_vision,
            get_settings(),
            image_paths,
            prompt,
            system=system,
            resolution=resolution,
            call_type=call_type,
            product_id=product_id,
            avito_id=avito_id,
        )

    async def _gemini_vision(
        self,
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
    ) -> dict:
        return await execute_gemini_vision_flow(
            get_settings(),
            image_paths,
            prompt,
            system=system,
            key=key,
            resolution=resolution,
            call_type=call_type,
            product_id=product_id,
            avito_id=avito_id,
            model=model,
            preferred_model=LLMRouter.get_preferred_gemini_model(),
            is_circuit_open=self._is_gemini_circuit_open(),
            circuit_until=self._gemini_vision_circuit_until,
            record_result_fn=self._record_gemini_vision_result,
        )

