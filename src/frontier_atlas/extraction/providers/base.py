"""Provider abstraction + HTTP status -> error-type mapping.

All providers are called over plain HTTPS with httpx (no vendor SDKs) so that
error handling is uniform and we see the raw status codes and headers.
"""
from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass

import httpx

from ...core.errors import (
    FatalProviderError,
    InvalidOutputError,
    PayloadTooLargeError,
    ProviderError,
    RateLimitError,
    TransientError,
)
from ...core.rate_limiter import DualTokenBucket
from ...core.retry import parse_retry_after

CONTEXT_OVERFLOW_RE = re.compile(
    r"context[_ ]length|maximum context|reduce the length|too many tokens|"
    r"exceeds the maximum|request too large|entity too large|input token count|prompt is too long",
    re.I,
)
# provider-side JSON-mode validation failure: the model's output was not valid JSON
# (seen live from Groq gpt-oss: 400 json_validate_failed). Repairable, not fatal.
JSON_MODE_FAILED_RE = re.compile(r"json_validate_failed|failed to validate json|failed_generation", re.I)
GEMINI_RETRY_DELAY_RE = re.compile(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"')


@dataclass(slots=True)
class LLMResponse:
    text: str
    provider: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None


class LLMProvider(ABC):
    name: str = "base"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        max_input_tokens: int,
        rpm: int,
        tpm: int,
        client: httpx.AsyncClient,
        timeout_s: float = 60.0,
    ):
        self.api_key = api_key
        self.model = model
        self.max_input_tokens = max_input_tokens
        self.limiter = DualTokenBucket(rpm=rpm, tpm=tpm)
        self.client = client
        self.timeout_s = timeout_s

    @abstractmethod
    async def complete(self, system: str, user: str) -> LLMResponse:
        """Return the raw model text (expected to be a JSON object)."""

    # ------------------------------------------------------------------ helpers
    async def _post(self, url: str, *, headers: dict, payload: dict) -> dict:
        try:
            resp = await self.client.post(url, headers=headers, json=payload, timeout=self.timeout_s)
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            raise TransientError(f"{type(exc).__name__}: {exc}", provider=self.name) from exc
        self._raise_for_status(resp)
        try:
            return resp.json()
        except json.JSONDecodeError as exc:
            raise TransientError("non-JSON response body", provider=self.name, status=resp.status_code) from exc

    def _raise_for_status(self, resp: httpx.Response) -> None:
        status = resp.status_code
        if status < 400:
            return
        body = resp.text[:2000]
        kw = {"provider": self.name, "status": status}

        if status == 429:
            retry_after = parse_retry_after(resp.headers.get("retry-after"))
            if retry_after is None and (m := GEMINI_RETRY_DELAY_RE.search(body)):
                retry_after = float(m.group(1))
            raise RateLimitError(f"429 rate limited: {body[:300]}", retry_after=retry_after, **kw)
        if status == 413 or (status == 400 and CONTEXT_OVERFLOW_RE.search(body)):
            raise PayloadTooLargeError(f"{status} payload too large: {body[:300]}", **kw)
        if status == 400 and JSON_MODE_FAILED_RE.search(body):
            raise InvalidOutputError(f"{status} provider rejected its own JSON: {body[:300]}", **kw)
        if status in (401, 403, 404):
            raise FatalProviderError(f"{status}: {body[:300]}", **kw)
        if status >= 500 or status in (408, 409, 425):
            raise TransientError(f"{status}: {body[:300]}", **kw)
        raise ProviderError(f"{status}: {body[:300]}", **kw)
