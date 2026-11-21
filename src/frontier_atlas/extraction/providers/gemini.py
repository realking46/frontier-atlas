from __future__ import annotations

from ...core.errors import InvalidOutputError
from .base import LLMProvider, LLMResponse


class GeminiProvider(LLMProvider):
    name = "gemini"
    base_url = "https://generativelanguage.googleapis.com/v1beta"

    async def complete(self, system: str, user: str) -> LLMResponse:
        data = await self._post(
            f"{self.base_url}/models/{self.model}:generateContent",
            headers={"x-goog-api-key": self.api_key},
            payload={
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {
                    "temperature": 0,
                    "maxOutputTokens": 1500,
                    "responseMimeType": "application/json",
                },
            },
        )
        try:
            candidate = data["candidates"][0]
            text = "".join(p.get("text", "") for p in candidate["content"]["parts"])
        except (KeyError, IndexError, TypeError) as exc:
            # blocked by safety filters or empty candidate
            reason = (data.get("promptFeedback") or {}).get("blockReason") or str(data)[:300]
            raise InvalidOutputError(f"no candidate returned: {reason}", provider=self.name) from exc
        usage = data.get("usageMetadata") or {}
        return LLMResponse(
            text=text,
            provider=self.name,
            model=self.model,
            input_tokens=usage.get("promptTokenCount"),
            output_tokens=usage.get("candidatesTokenCount"),
        )
