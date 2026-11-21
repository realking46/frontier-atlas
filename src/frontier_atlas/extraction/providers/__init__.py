"""Provider registry: builds the fallback chain from settings."""
from __future__ import annotations

import httpx

from ...core.config import Settings
from .base import LLMProvider, LLMResponse
from .deepseek import DeepSeekProvider
from .gemini import GeminiProvider
from .groq import GroqProvider

__all__ = ["LLMProvider", "LLMResponse", "build_chain", "GeminiProvider", "GroqProvider", "DeepSeekProvider"]


def build_chain(settings: Settings, client: httpx.AsyncClient) -> list[LLMProvider]:
    """Providers without an API key are silently skipped."""
    specs = {
        "gemini": (GeminiProvider, settings.gemini_api_key, settings.gemini_model,
                   settings.gemini_max_input_tokens, settings.gemini_rpm, settings.gemini_tpm),
        "groq": (GroqProvider, settings.groq_api_key, settings.groq_model,
                 settings.groq_max_input_tokens, settings.groq_rpm, settings.groq_tpm),
        "deepseek": (DeepSeekProvider, settings.deepseek_api_key, settings.deepseek_model,
                     settings.deepseek_max_input_tokens, settings.deepseek_rpm, settings.deepseek_tpm),
    }
    chain: list[LLMProvider] = []
    for name in settings.chain:
        if name not in specs:
            raise ValueError(f"unknown provider in FA_LLM_CHAIN: {name}")
        cls, key, model, max_in, rpm, tpm = specs[name]
        if not key:
            continue
        chain.append(
            cls(api_key=key, model=model, max_input_tokens=max_in, rpm=rpm, tpm=tpm,
                client=client, timeout_s=settings.llm_timeout_s)
        )
    return chain
