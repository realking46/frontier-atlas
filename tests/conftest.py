from __future__ import annotations

import json

import httpx
import pytest

from frontier_atlas.core.config import Settings
from frontier_atlas.extraction.providers.base import LLMProvider, LLMResponse


def make_settings(**overrides) -> Settings:
    base = dict(
        llm_concurrency=4,
        llm_max_retries=2,
        llm_max_429_retries=2,
        llm_max_shrinks=3,
        llm_backoff_base_s=0.01,
        llm_backoff_cap_s=0.05,
        llm_provider_cooldown_s=60,
    )
    base.update(overrides)
    return Settings(_env_file=None, **base)


class FakeProvider(LLMProvider):
    """Plays back a script of responses: str -> model text, Exception -> raised."""

    def __init__(self, name: str, script: list, max_input_tokens: int = 4000):
        self.name = name
        super().__init__(
            api_key="x", model=f"{name}-model", max_input_tokens=max_input_tokens,
            rpm=10_000, tpm=10_000_000, client=httpx.AsyncClient(),
        )
        self.script = list(script)
        self.prompts: list[str] = []

    async def complete(self, system: str, user: str) -> LLMResponse:
        self.prompts.append(user)
        item = self.script.pop(0) if self.script else self.default()
        if isinstance(item, Exception):
            raise item
        return LLMResponse(text=item, provider=self.name, model=self.model)

    def default(self):
        raise AssertionError(f"{self.name}: script exhausted")


def as_json(**kw) -> str:
    return json.dumps(kw)


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def sleeps():
    calls: list[float] = []

    async def fake_sleep(s: float) -> None:
        calls.append(s)

    fake_sleep.calls = calls
    return fake_sleep


PAGE = """
[title] Acme Robotics – AI Startup Profile
Acme Robotics builds autonomous warehouse robots powered by foundation models.
Founded in 2019, the company has 120 employees and is headquartered in Berlin, Germany.
Website: https://acmerobotics.ai
"""
