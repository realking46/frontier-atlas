"""Multi-tier LLM extraction engine.

    page text ──► fit_to_budget (per provider) ──► provider.complete()
                         ▲            │
                         │  413 ──────┘ shrink budget ×0.55, same provider
                         │
   429 ─► pause provider's limiter, backoff+jitter (honour Retry-After),
          after N tries put provider in cooldown and FALL THROUGH
   5xx/timeout ─► backoff+jitter, same provider
   bad JSON / schema / hallucinated identity ─► 1 repair call with feedback,
          then FALL THROUGH
   401/403/404 ─► disable provider for the process, FALL THROUGH
   model says "no entity" ─► stop the chain (valid empty answer)

The orchestrator never fabricates: every returned object passed the grounding
gate against the original page text.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Generic, TypeVar

from pydantic import ValidationError

from ..core.config import Settings, get_settings
from ..core.errors import (
    AllProvidersFailedError,
    FatalProviderError,
    InvalidOutputError,
    NoEntityFoundError,
    PayloadTooLargeError,
    ProviderError,
    RateLimitError,
    TransientError,
)
from ..core.logging import get_logger
from ..core.retry import backoff_delay
from ..schemas.base import ExtractionContent
from . import grounding
from .chunker import ChunkResult, count_tokens, fit_to_budget
from .prompts import system_prompt, user_prompt
from .providers.base import LLMProvider

log = get_logger(__name__)
T = TypeVar("T", bound=ExtractionContent)

OUTPUT_RESERVE_TOKENS = 1500   # room for the model's answer
PROMPT_OVERHEAD_TOKENS = 150   # wrapper text in user_prompt + feedback


@dataclass(slots=True)
class Attempt:
    provider: str
    outcome: str          # ok | 429 | 413 | transient | invalid | fatal | cooldown
    input_tokens: int = 0
    detail: str = ""


@dataclass(slots=True)
class ExtractionResult(Generic[T]):
    data: T
    provider: str
    model: str
    chunk: ChunkResult
    dropped_fields: list[str]
    attempts: list[Attempt] = field(default_factory=list)


class LLMOrchestrator:
    def __init__(
        self,
        providers: list[LLMProvider],
        settings: Settings | None = None,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        if not providers:
            raise ValueError("no LLM providers configured - set GEMINI_API_KEY, GROQ_API_KEY or DEEPSEEK_API_KEY in .env")
        self.providers = providers
        self.s = settings or get_settings()
        self._sleep = sleep
        self._sem = asyncio.Semaphore(self.s.llm_concurrency)
        self._cooldown_until: dict[str, float] = {}
        self._disabled: set[str] = set()
        self.stats: Counter[str] = Counter()

    # ------------------------------------------------------------------ public
    async def extract(
        self,
        page_text: str,
        schema_cls: type[T],
        *,
        source_url: str | None = None,
    ) -> ExtractionResult[T]:
        attempts: list[Attempt] = []
        errors: list[tuple[str, Exception]] = []
        async with self._sem:
            for provider in self.providers:
                if provider.name in self._disabled:
                    continue
                if (left := self._cooldown_until.get(provider.name, 0) - time.monotonic()) > 0:
                    attempts.append(Attempt(provider.name, "cooldown", detail=f"{left:.0f}s left"))
                    continue
                try:
                    result = await self._run_provider(provider, page_text, schema_cls, source_url, attempts)
                    self.stats[f"{provider.name}.ok"] += 1
                    return result
                except NoEntityFoundError:
                    self.stats["no_entity"] += 1
                    raise
                except FatalProviderError as exc:
                    self._disabled.add(provider.name)
                    log.error("llm.provider_disabled", provider=provider.name, error=str(exc))
                    errors.append((provider.name, exc))
                except RateLimitError as exc:
                    self._cooldown_until[provider.name] = time.monotonic() + self.s.llm_provider_cooldown_s
                    log.warning("llm.provider_cooldown", provider=provider.name,
                                seconds=self.s.llm_provider_cooldown_s)
                    errors.append((provider.name, exc))
                except ProviderError as exc:  # 413 exhausted, transient exhausted, invalid output
                    errors.append((provider.name, exc))
                self.stats[f"{provider.name}.fallthrough"] += 1
                log.warning("llm.fallthrough", provider=provider.name, error=type(errors[-1][1]).__name__)
        self.stats["all_failed"] += 1
        if not errors:
            errors.append(("*", RuntimeError("every provider is disabled or cooling down")))
        raise AllProvidersFailedError(errors)

    # ------------------------------------------------------------------ per provider loop
    async def _run_provider(
        self,
        p: LLMProvider,
        page_text: str,
        schema_cls: type[T],
        source_url: str | None,
        attempts: list[Attempt],
    ) -> ExtractionResult[T]:
        kind = schema_cls.prompt_name
        system = system_prompt(schema_cls)
        sys_tokens = count_tokens(system)
        budget = p.max_input_tokens - sys_tokens - PROMPT_OVERHEAD_TOKENS
        max_bytes = self.s.llm_max_request_bytes - len(system.encode()) - 2_000
        if budget < 200:
            raise PayloadTooLargeError("system prompt alone exceeds budget", provider=p.name)

        shrinks = rl_retries = transient_retries = repairs = 0
        feedback: str | None = None

        while True:
            chunk = fit_to_budget(page_text, budget, kind=kind, max_bytes=max_bytes)
            user = user_prompt(chunk.text, source_url, feedback)
            est = sys_tokens + count_tokens(user) + OUTPUT_RESERVE_TOKENS
            await p.limiter.acquire(est)
            self.stats[f"{p.name}.calls"] += 1
            try:
                resp = await p.complete(system, user)
                data = self._parse(resp.text, schema_cls, p.name)
                report = grounding.check(data, page_text, kind=kind, min_score=self.s.grounding_min_score)
                if not report.ok:
                    if grounding.IDENTITY_FIELD.get(kind) not in report.dropped:
                        # model honestly returned null identity: nothing to extract here
                        raise NoEntityFoundError(f"{kind}: {report.reason}")
                    raise InvalidOutputError(f"hallucination guard: {report.reason}", provider=p.name)
                attempts.append(Attempt(p.name, "ok", chunk.tokens))
                if report.dropped:
                    log.info("llm.fields_dropped", provider=p.name, fields=report.dropped, url=source_url)
                return ExtractionResult(data, p.name, resp.model, chunk, report.dropped, attempts)

            except PayloadTooLargeError as exc:
                attempts.append(Attempt(p.name, "413", chunk.tokens, str(exc)[:120]))
                self.stats[f"{p.name}.413"] += 1
                shrinks += 1
                if shrinks > self.s.llm_max_shrinks:
                    raise
                budget = max(200, int(min(budget, chunk.tokens) * self.s.llm_shrink_factor))
                max_bytes = int(max_bytes * self.s.llm_shrink_factor)
                log.info("llm.shrink", provider=p.name, new_budget=budget, attempt=shrinks)

            except RateLimitError as exc:
                attempts.append(Attempt(p.name, "429", chunk.tokens, f"retry_after={exc.retry_after}"))
                self.stats[f"{p.name}.429"] += 1
                if rl_retries >= self.s.llm_max_429_retries:
                    raise
                delay = backoff_delay(rl_retries, base=self.s.llm_backoff_base_s,
                                      cap=self.s.llm_backoff_cap_s, retry_after=exc.retry_after)
                p.limiter.pause(delay)  # every task on this provider backs off, not just us
                rl_retries += 1
                log.info("llm.backoff", provider=p.name, reason="429", sleep=round(delay, 2))
                await self._sleep(delay)

            except TransientError as exc:
                attempts.append(Attempt(p.name, "transient", chunk.tokens, str(exc)[:120]))
                self.stats[f"{p.name}.transient"] += 1
                if transient_retries >= self.s.llm_max_retries:
                    raise
                delay = backoff_delay(transient_retries, base=self.s.llm_backoff_base_s,
                                      cap=self.s.llm_backoff_cap_s)
                transient_retries += 1
                log.info("llm.backoff", provider=p.name, reason="transient", sleep=round(delay, 2))
                await self._sleep(delay)

            except InvalidOutputError as exc:
                attempts.append(Attempt(p.name, "invalid", chunk.tokens, str(exc)[:120]))
                self.stats[f"{p.name}.invalid"] += 1
                if repairs >= 1:
                    raise
                repairs += 1
                feedback = str(exc)[:500]

            except FatalProviderError as exc:
                attempts.append(Attempt(p.name, "fatal", chunk.tokens, str(exc)[:120]))
                raise

    # ------------------------------------------------------------------ parsing
    @staticmethod
    def _parse(text: str, schema_cls: type[T], provider: str) -> T:
        obj = parse_json_object(text)
        if obj is None:
            raise InvalidOutputError(f"not a JSON object: {text[:200]!r}", provider=provider)
        keys = set(schema_cls.model_fields)
        # unwrap {"result": {...}} style answers
        if not keys & obj.keys() and len(obj) == 1 and isinstance(next(iter(obj.values())), dict):
            obj = next(iter(obj.values()))
        try:
            return schema_cls.model_validate(obj)
        except ValidationError as exc:
            raise InvalidOutputError(f"schema validation failed: {exc.errors()[:3]}", provider=provider) from exc


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.I | re.M)


def parse_json_object(text: str) -> dict | None:
    text = _FENCE_RE.sub("", text.strip())
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            obj = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    if isinstance(obj, list) and obj and isinstance(obj[0], dict):
        obj = obj[0]
    return obj if isinstance(obj, dict) else None
