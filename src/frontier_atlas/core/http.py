"""Shared async HTTP fetcher for all crawlers.

* one pooled httpx.AsyncClient (keep-alive, connection limit)
* per-host concurrency cap + minimum interval (politeness, API terms)
* retries with full-jitter backoff on 429 / 5xx / network errors
* honours Retry-After and the IETF `RateLimit` header (used by Hugging Face:
  `"api";r=<remaining>;t=<seconds-to-reset>`) and GitHub's X-RateLimit-* headers,
  pausing the whole host *before* we get blocked.
"""
from __future__ import annotations

import asyncio
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

from .config import Settings, get_settings
from .logging import get_logger
from .retry import backoff_delay, parse_retry_after

log = get_logger(__name__)

RETRY_STATUSES = {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 524}
_IETF_RL_RE = re.compile(r"r=(\d+);\s*t=(\d+)")


class FetchError(Exception):
    def __init__(self, url: str, status: int | None, message: str):
        super().__init__(f"{status} {url}: {message}")
        self.url = url
        self.status = status


@dataclass
class _HostState:
    sem: asyncio.Semaphore
    min_interval: float
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    next_slot: float = 0.0
    paused_until: float = 0.0


class HttpFetcher:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        host_intervals: dict[str, float] | None = None,
        client: httpx.AsyncClient | None = None,
    ):
        self.s = settings or get_settings()
        self.client = client or httpx.AsyncClient(
            timeout=self.s.http_timeout_s,
            follow_redirects=True,
            headers={"User-Agent": self.s.user_agent, "Accept-Encoding": "gzip, deflate"},
            limits=httpx.Limits(max_connections=self.s.http_max_connections,
                                max_keepalive_connections=self.s.http_max_connections),
        )
        self._host_intervals = {
            "export.arxiv.org": self.s.arxiv_min_interval_s,
            "paperswithcode.co": self.s.pwc_min_interval_s,
            **(host_intervals or {}),
        }
        self._hosts: dict[str, _HostState] = {}
        self.stats: defaultdict[str, int] = defaultdict(int)

    async def __aenter__(self) -> "HttpFetcher":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.client.aclose()

    # ------------------------------------------------------------------ host bookkeeping
    def set_host_interval(self, url_or_host: str, seconds: float) -> None:
        host = urlparse(url_or_host).netloc.lower() if "://" in url_or_host else url_or_host.lower()
        self._host_intervals[host] = max(self._host_intervals.get(host, 0.0), seconds)
        if host in self._hosts:
            self._hosts[host].min_interval = self._host_intervals[host]

    def _host(self, url: str) -> _HostState:
        host = urlparse(url).netloc.lower()
        if host not in self._hosts:
            interval = self._host_intervals.get(host, self.s.http_min_interval_s)
            conc = 1 if interval >= 1 else self.s.http_per_host_concurrency
            self._hosts[host] = _HostState(asyncio.Semaphore(conc), interval)
        return self._hosts[host]

    async def _wait_turn(self, h: _HostState) -> None:
        async with h.lock:
            now = time.monotonic()
            start = max(now, h.next_slot, h.paused_until)
            h.next_slot = start + h.min_interval
        if (delay := start - time.monotonic()) > 0:
            await asyncio.sleep(delay)

    def _observe_rate_headers(self, h: _HostState, resp: httpx.Response) -> None:
        # IETF draft header (Hugging Face)
        if m := _IETF_RL_RE.search(resp.headers.get("ratelimit", "")):
            remaining, reset = int(m.group(1)), int(m.group(2))
            if remaining <= 5:
                h.paused_until = max(h.paused_until, time.monotonic() + reset + 1)
                log.warning("http.host_paused", host=resp.url.host, seconds=reset, reason="ratelimit header")
        # GitHub style
        rem = resp.headers.get("x-ratelimit-remaining")
        reset_at = resp.headers.get("x-ratelimit-reset")
        if rem is not None and reset_at and int(rem) <= 0:
            wait = max(0.0, int(reset_at) - time.time()) + 1
            h.paused_until = max(h.paused_until, time.monotonic() + wait)
            log.warning("http.host_paused", host=resp.url.host, seconds=round(wait), reason="x-ratelimit")

    # ------------------------------------------------------------------ public
    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
        headers: dict | None = None,
        ok_statuses: tuple[int, ...] = (200,),
        max_retries: int | None = None,
    ) -> httpx.Response:
        h = self._host(url)
        retries = self.s.http_max_retries if max_retries is None else max_retries
        attempt = 0
        async with h.sem:
            while True:
                await self._wait_turn(h)
                self.stats["requests"] += 1
                try:
                    resp = await self.client.request(method, url, params=params, json=json, headers=headers)
                except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                    status, retry_after, msg = None, None, f"{type(exc).__name__}: {exc}"
                else:
                    self._observe_rate_headers(h, resp)
                    if resp.status_code in ok_statuses:
                        return resp
                    status = resp.status_code
                    retry_after = parse_retry_after(resp.headers.get("retry-after"))
                    msg = resp.text[:200]
                    is_gh_ratelimit = status == 403 and resp.headers.get("x-ratelimit-remaining") == "0"
                    if status not in RETRY_STATUSES and not is_gh_ratelimit:
                        self.stats[f"http_{status}"] += 1
                        raise FetchError(url, status, msg)
                self.stats[f"retry_{status or 'net'}"] += 1
                if attempt >= retries:
                    raise FetchError(url, status, f"gave up after {attempt + 1} tries: {msg}")
                delay = backoff_delay(attempt, base=1.0, cap=60.0, retry_after=retry_after)
                if status == 429:
                    h.paused_until = max(h.paused_until, time.monotonic() + delay)
                log.info("http.retry", url=url, status=status, attempt=attempt + 1, sleep=round(delay, 2))
                await asyncio.sleep(delay)
                attempt += 1

    async def get(self, url: str, **kw) -> httpx.Response:
        return await self.request("GET", url, **kw)

    async def get_json(self, url: str, **kw):
        return (await self.get(url, **kw)).json()

    async def get_text(self, url: str, **kw) -> str:
        return (await self.get(url, **kw)).text
