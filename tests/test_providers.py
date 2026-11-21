"""HTTP-level tests: real provider classes against a mocked transport."""
from __future__ import annotations

import random

import httpx
import pytest

from frontier_atlas.core.errors import (
    FatalProviderError,
    PayloadTooLargeError,
    RateLimitError,
    TransientError,
)
from frontier_atlas.core.retry import backoff_delay, parse_retry_after
from frontier_atlas.extraction.providers import GeminiProvider, GroqProvider


def _provider(cls, handler):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return cls(api_key="k", model="m", max_input_tokens=1000, rpm=100, tpm=100_000, client=client)


@pytest.mark.parametrize(
    "status,body,headers,exc",
    [
        (429, "slow down", {"retry-after": "7"}, RateLimitError),
        (413, "Request Entity Too Large", {}, PayloadTooLargeError),
        (400, '{"error":{"code":"context_length_exceeded"}}', {}, PayloadTooLargeError),
        (401, "invalid api key", {}, FatalProviderError),
        (503, "overloaded", {}, TransientError),
    ],
)
async def test_groq_status_mapping(status, body, headers, exc):
    p = _provider(GroqProvider, lambda req: httpx.Response(status, text=body, headers=headers))
    with pytest.raises(exc) as ei:
        await p.complete("s", "u")
    if exc is RateLimitError:
        assert ei.value.retry_after == 7.0


async def test_groq_success_and_request_shape():
    seen = {}

    def handler(req: httpx.Request):
        seen["url"] = str(req.url)
        seen["auth"] = req.headers["authorization"]
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"a":1}'}}],
                                         "usage": {"prompt_tokens": 10, "completion_tokens": 3}})

    r = await _provider(GroqProvider, handler).complete("s", "u")
    assert r.text == '{"a":1}' and r.input_tokens == 10
    assert seen["url"].endswith("/openai/v1/chat/completions")
    assert seen["auth"] == "Bearer k"


async def test_gemini_429_retry_delay_from_body():
    body = '{"error":{"code":429,"details":[{"@type":"type.googleapis.com/google.rpc.RetryInfo","retryDelay":"31s"}]}}'
    p = _provider(GeminiProvider, lambda req: httpx.Response(429, text=body))
    with pytest.raises(RateLimitError) as ei:
        await p.complete("s", "u")
    assert ei.value.retry_after == 31.0


async def test_gemini_success():
    payload = {"candidates": [{"content": {"parts": [{"text": '{"x":'}, {"text": "2}"}]}}],
               "usageMetadata": {"promptTokenCount": 5}}
    r = await _provider(GeminiProvider, lambda req: httpx.Response(200, json=payload)).complete("s", "u")
    assert r.text == '{"x":2}'


def test_backoff_full_jitter_bounds():
    rng = random.Random(0)
    for attempt in range(8):
        d = backoff_delay(attempt, base=1, cap=30, rng=rng)
        assert 0 <= d <= min(30, 2**attempt)
    assert backoff_delay(0, base=1, cap=30, retry_after=10, rng=rng) >= 10


def test_parse_retry_after():
    assert parse_retry_after("5") == 5.0
    assert parse_retry_after(None) is None
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0  # in the past
