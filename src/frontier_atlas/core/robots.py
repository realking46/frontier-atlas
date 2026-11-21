"""robots.txt compliance, cached per host.

Checked before every page fetch of the protected-fetch path. A robots.txt that
cannot be fetched (network error, 5xx) means "unknown": we allow but do not
cache for long; a 4xx means no rules (allowed), per RFC 9309.
"""
from __future__ import annotations

import asyncio
import time
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

from .logging import get_logger

log = get_logger(__name__)


class RobotsCache:
    def __init__(self, client: httpx.AsyncClient, user_agent: str, ttl_s: float = 6 * 3600):
        self.client = client
        self.ua = user_agent
        self.ttl = ttl_s
        self._rules: dict[str, tuple[float, RobotFileParser | None]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def _load(self, origin: str) -> RobotFileParser | None:
        rp = RobotFileParser()
        try:
            r = await self.client.get(f"{origin}/robots.txt", timeout=15, follow_redirects=True)
        except httpx.HTTPError:
            return None                                   # unknown -> allow, retry soon
        if 400 <= r.status_code < 500:
            rp.parse([])                                  # no robots.txt -> everything allowed
        elif r.status_code >= 500:
            return None
        else:
            rp.parse(r.text.splitlines())
        return rp

    async def allowed(self, url: str) -> bool:
        p = urlparse(url)
        origin = f"{p.scheme}://{p.netloc}"
        lock = self._locks.setdefault(origin, asyncio.Lock())
        async with lock:
            hit = self._rules.get(origin)
            if hit is None or hit[0] < time.monotonic():
                rp = await self._load(origin)
                self._rules[origin] = (time.monotonic() + (self.ttl if rp else 300), rp)
                hit = self._rules[origin]
        rp = hit[1]
        ok = True if rp is None else rp.can_fetch(self.ua, url)
        if not ok:
            log.info("robots.disallowed", url=url)
        return ok

    async def crawl_delay(self, url: str) -> float | None:
        await self.allowed(url)
        p = urlparse(url)
        rp = self._rules.get(f"{p.scheme}://{p.netloc}", (0, None))[1]
        d = rp.crawl_delay(self.ua) if rp else None
        return float(d) if d is not None else None
