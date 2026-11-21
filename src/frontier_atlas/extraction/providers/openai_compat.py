"""Groq and DeepSeek both expose an OpenAI-compatible /chat/completions API."""
from __future__ import annotations

from ...core.errors import InvalidOutputError
from .base import LLMProvider, LLMResponse


class OpenAICompatProvider(LLMProvider):
    base_url: str = ""
    reasoning_effort: str | None = None   # reasoning models (Groq gpt-oss): reasoning counts against max_tokens

    async def complete(self, system: str, user: str) -> LLMResponse:
        payload = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": 1500,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        data = await self._post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            payload=payload,
        )
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise InvalidOutputError(f"unexpected response shape: {str(data)[:300]}", provider=self.name) from exc
        usage = data.get("usage") or {}
        return LLMResponse(
            text=text,
            provider=self.name,
            model=self.model,
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
        )
