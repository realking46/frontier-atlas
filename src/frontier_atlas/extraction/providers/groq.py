from __future__ import annotations

from .openai_compat import OpenAICompatProvider


class GroqProvider(OpenAICompatProvider):
    """Groq-hosted Llama 3.x. Note: Groq returns 413 when a single request
    exceeds the org's tokens-per-minute limit, which our shrink loop handles."""

    name = "groq"
    base_url = "https://api.groq.com/openai/v1"
