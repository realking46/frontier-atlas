"""Exponential backoff with *full jitter* (AWS architecture blog variant).

sleep = random(0, min(cap, base * 2**attempt))

Full jitter de-synchronises thousands of concurrent workers that all hit a 429
at the same moment, so they don't retry in lock-step and re-trigger the limit.
If the server sends Retry-After we never sleep less than that.
"""
from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from typing import TypeVar

T = TypeVar("T")


def backoff_delay(
    attempt: int,
    *,
    base: float = 1.0,
    cap: float = 60.0,
    retry_after: float | None = None,
    rng: random.Random | None = None,
) -> float:
    r = rng or random
    delay = r.uniform(0, min(cap, base * (2**attempt)))
    if retry_after is not None:
        # small jitter on top of Retry-After so workers don't wake up together
        delay = max(delay, retry_after + r.uniform(0, min(1.0, retry_after * 0.1 + 0.1)))
    return delay


def parse_retry_after(value: str | None) -> float | None:
    """Retry-After may be delta-seconds or an HTTP date."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return None


async def retry_async(
    fn: Callable[[], Awaitable[T]],
    *,
    retry_on: tuple[type[BaseException], ...],
    max_retries: int = 4,
    base: float = 1.0,
    cap: float = 60.0,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """Generic helper used by fetchers. The LLM orchestrator has its own loop
    because it must treat 413 and 429 differently."""
    attempt = 0
    while True:
        try:
            return await fn()
        except retry_on as exc:
            if attempt >= max_retries:
                raise
            delay = backoff_delay(
                attempt, base=base, cap=cap, retry_after=getattr(exc, "retry_after", None)
            )
            if on_retry:
                on_retry(attempt, exc, delay)
            await sleep(delay)
            attempt += 1
