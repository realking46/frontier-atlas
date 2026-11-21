"""Client-side rate limiting so we rarely see a 429 in the first place.

Each provider gets a DualTokenBucket: one bucket for requests/min and one for
tokens/min. A call reserves (1 request, N estimated tokens) and waits until
both buckets can pay. This is per-process; in a multi-node deployment the same
interface is backed by Redis (GCRA / redis-cell) so all nodes share one budget.
"""
from __future__ import annotations

import asyncio
import time


class TokenBucket:
    def __init__(self, capacity: float, refill_per_sec: float):
        self.capacity = float(capacity)
        self.refill_per_sec = float(refill_per_sec)
        self.tokens = float(capacity)
        self.updated = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.refill_per_sec)
        self.updated = now

    def wait_time(self, amount: float) -> float:
        self._refill()
        amount = min(amount, self.capacity)  # a single huge request can still go through eventually
        if self.tokens >= amount:
            return 0.0
        return (amount - self.tokens) / self.refill_per_sec

    def consume(self, amount: float) -> None:
        self.tokens -= min(amount, self.capacity)


class DualTokenBucket:
    def __init__(self, rpm: int, tpm: int):
        self.requests = TokenBucket(rpm, rpm / 60.0)
        self.tokens = TokenBucket(tpm, tpm / 60.0)
        self._lock = asyncio.Lock()
        self._paused_until = 0.0

    def pause(self, seconds: float) -> None:
        """Called when the server returns 429 – everyone on this provider waits."""
        self._paused_until = max(self._paused_until, time.monotonic() + seconds)

    @property
    def paused_for(self) -> float:
        return max(0.0, self._paused_until - time.monotonic())

    async def acquire(self, est_tokens: int) -> None:
        async with self._lock:
            while True:
                wait = max(
                    self.paused_for,
                    self.requests.wait_time(1),
                    self.tokens.wait_time(est_tokens),
                )
                if wait <= 0:
                    self.requests.consume(1)
                    self.tokens.consume(est_tokens)
                    return
                await asyncio.sleep(min(wait, 5.0))
