"""Tiered fetching for sites that defend against bots.

    tier 0  robots.txt          disallowed -> never fetched
    tier 1  plain HTTP           async httpx, per-host politeness, retry/backoff (HttpFetcher)
    tier 2  real browser         Playwright Chromium, only when tier 1 met a JS challenge,
                                 a JS-only app shell, or a bare WAF denial
    stop    captcha / hard block circuit opens for the host; caller uses an alternative channel

Per-host memory makes this cheap at scale:
  * sticky tier: once a host needed the browser, later URLs go straight to it
    (no wasted HTTP request that is guaranteed to be challenged);
  * circuit breaker: a terminal block (captcha / hard block) or N consecutive
    blocks open the circuit for a cool-down that doubles each time, so a
    blocking site costs one request per cool-down instead of thousands;
  * slow-down: every block doubles the host's minimum request interval
    (multiplicative decrease of rate), which is what keeps a crawl under a
    site's rate-based bot score in the first place;
  * robots.txt crawl-delay is applied as the host's minimum interval;
  * probe-first: the first request to a host runs alone, so a wall is hit once;
  * vendor learning: when the browser tier keeps failing one vendor's challenge
    (and never passes it), that vendor's challenges stop being escalated.

`stats` records, per host, how many pages came from which tier and which
blocks were seen; it is printed in run summaries.
"""
from __future__ import annotations

import asyncio
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from urllib.parse import urlparse

from .antibot import Block, BlockKind, classify
from .browser import BrowserRenderer
from .http import FetchError, HttpFetcher
from .logging import get_logger
from .robots import RobotsCache

log = get_logger(__name__)

# let HttpFetcher keep retrying genuinely transient statuses; return everything else for classification
_TRANSIENT = {408, 425, 429, 500, 502, 504, 520, 521, 522, 524}
_CLASSIFY_STATUSES = tuple(s for s in range(200, 600) if s not in _TRANSIENT)


class Blocked(FetchError):
    def __init__(self, url: str, block: Block, status: int | None = None, note: str = ""):
        super().__init__(url, status, f"{block.kind.value} ({block.vendor}) {note}".strip())
        self.block = block


@dataclass
class Page:
    url: str
    final_url: str
    status: int
    html: str
    headers: dict[str, str]
    tier: str                       # "http" | "browser"

    @property
    def text(self) -> str:          # same attribute name as httpx.Response
        return self.html


@dataclass
class _Host:
    tier: str = "http"
    consecutive_blocks: int = 0
    open_until: float = 0.0
    openings: int = 0
    last_block: Block = field(default_factory=lambda: Block(BlockKind.NONE))
    delay_applied: bool = False
    probe: asyncio.Lock = field(default_factory=asyncio.Lock)
    probed: bool = False            # first response from this host seen -> concurrency allowed


class ProtectedFetcher:
    def __init__(
        self,
        http: HttpFetcher,
        browser: BrowserRenderer | None = None,
        *,
        robots: RobotsCache | bool = True,
        breaker_threshold: int = 3,
        breaker_cooldown_s: float = 600.0,
        block_slowdown_s: float = 2.0,
        vendor_giveup_after: int = 5,
    ):
        self.http = http
        self.browser = browser
        if robots is True:
            robots = RobotsCache(http.client, http.s.user_agent)
        self.robots: RobotsCache | None = robots or None
        self.breaker_threshold = breaker_threshold
        self.breaker_cooldown_s = breaker_cooldown_s
        self.block_slowdown_s = block_slowdown_s
        self._hosts: dict[str, _Host] = defaultdict(_Host)
        # browser-tier track record per protection vendor: [passed, failed]
        self.vendor_record: defaultdict[str, list[int]] = defaultdict(lambda: [0, 0])
        self.vendor_giveup_after = vendor_giveup_after
        self._escalated_vendor: dict[str, str] = {}
        self.stats: defaultdict[str, Counter] = defaultdict(Counter)

    # ------------------------------------------------------------------ host state
    def _open_circuit(self, host: str, h: _Host, block: Block) -> None:
        h.openings += 1
        cooldown = self.breaker_cooldown_s * 2 ** (h.openings - 1)
        h.open_until = time.monotonic() + cooldown
        log.warning("antibot.circuit_open", host=host, kind=block.kind.value, vendor=block.vendor,
                    cooldown_s=round(cooldown))

    def _on_block(self, host: str, h: _Host, block: Block, url: str) -> None:
        h.consecutive_blocks += 1
        h.last_block = block
        self.stats[host][f"block:{block.kind.value}"] += 1
        # multiplicative slow-down of this host
        cur = self.http._host_intervals.get(host, 0.0)
        self.http.set_host_interval(host, min(60.0, max(self.block_slowdown_s, cur * 2)))
        if block.kind.terminal or h.consecutive_blocks >= self.breaker_threshold:
            self._open_circuit(host, h, block)
        log.info("antibot.blocked", url=url, kind=block.kind.value, vendor=block.vendor, tier=h.tier)

    def _worth_escalating(self, block: Block) -> bool:
        """Vendor learning: if the browser tier has failed this vendor's challenge N times and
        never passed it in this run, further escalations only cost ~15 s of browser time each."""
        passed, failed = self.vendor_record[block.vendor or "unknown"]
        if passed == 0 and failed >= self.vendor_giveup_after:
            self.stats["_vendors"][f"no_escalation:{block.vendor}"] += 1
            return False
        return True

    def host_state(self, url_or_host: str) -> _Host:
        host = urlparse(url_or_host).netloc.lower() if "://" in url_or_host else url_or_host
        return self._hosts[host]

    # ------------------------------------------------------------------ fetch
    async def fetch(self, url: str, *, max_retries: int | None = None) -> Page:
        """Probe-first: the first request to a host runs alone; concurrent requests for the
        same host wait for its outcome, so a captcha wall is hit once, not N times at once."""
        host = urlparse(url).netloc.lower()
        h = self._hosts[host]
        if not h.probed:
            async with h.probe:
                if not h.probed:
                    try:
                        return await self._fetch(url, host, h, max_retries)
                    finally:
                        h.probed = True
        return await self._fetch(url, host, h, max_retries)

    async def _fetch(self, url: str, host: str, h: _Host, max_retries: int | None) -> Page:
        if h.open_until > time.monotonic():
            self.stats[host]["skipped:circuit_open"] += 1
            raise Blocked(url, h.last_block, note="circuit open")

        if self.robots is not None:
            if not await self.robots.allowed(url):
                self.stats[host]["skipped:robots"] += 1
                raise Blocked(url, Block(BlockKind.ROBOTS, "robots.txt"))
            if not h.delay_applied:
                h.delay_applied = True
                if (d := await self.robots.crawl_delay(url)):
                    self.http.set_host_interval(host, min(d, 30.0))

        if h.tier == "http":
            resp = await self.http.request("GET", url, ok_statuses=_CLASSIFY_STATUSES, max_retries=max_retries)
            text = resp.text
            block = classify(resp.status_code, resp.headers, text)
            if not block:
                return self._ok(url, host, h, "http", resp.status_code, str(resp.url), text, dict(resp.headers))
            if block.kind is BlockKind.RATE_LIMITED:
                raise FetchError(url, resp.status_code, "rate limited (transient)")
            if not (block.kind.browser_may_help and self.browser is not None and self._worth_escalating(block)):
                self._on_block(host, h, block, url)
                raise Blocked(url, block, resp.status_code)
            self._escalated_vendor[url] = block.vendor or "unknown"
            log.info("antibot.escalate", host=host, kind=block.kind.value, vendor=block.vendor)
            self.stats[host][f"escalated:{block.kind.value}"] += 1
            h.tier = "browser"                       # sticky: the next URL on this host skips tier 1

        try:
            r = await self.browser.render(url)
        except Exception as exc:                     # navigation timeout, crashed page, ...
            self.stats[host]["browser_error"] += 1
            raise FetchError(url, None, f"browser: {type(exc).__name__}: {exc}"[:300]) from exc
        vendor = self._escalated_vendor.pop(url, None)
        if r.block:
            if vendor is not None:
                self.vendor_record[vendor][1] += 1
            self._on_block(host, h, r.block, url)
            raise Blocked(url, r.block, r.status)
        if vendor is not None:
            self.vendor_record[vendor][0] += 1
        return self._ok(url, host, h, "browser", r.status, r.url, r.html, r.headers)

    def _ok(self, url, host, h, tier, status, final_url, html, headers) -> Page:
        if status >= 400:
            # an ordinary error page (404 etc.), not a block
            self.stats[host][f"http_{status}"] += 1
            raise FetchError(url, status, "not found" if status == 404 else "error status")
        h.consecutive_blocks = 0
        self.stats[host][f"ok:{tier}"] += 1
        return Page(url, final_url, status, html, headers, tier)

    @classmethod
    def from_settings(cls, http: HttpFetcher) -> "ProtectedFetcher":
        s = http.s
        browser = None
        if s.browser_enabled and BrowserRenderer.available():
            browser = BrowserRenderer(max_pages=s.browser_max_pages, challenge_wait_s=s.browser_challenge_wait_s)
        return cls(http, browser, robots=s.robots_enabled, breaker_threshold=s.breaker_threshold,
                   breaker_cooldown_s=s.breaker_cooldown_s)

    async def close(self) -> None:
        if self.browser is not None:
            await self.browser.close()

    def summary(self) -> dict:
        out = {host: dict(c) for host, c in sorted(self.stats.items())}
        if self.vendor_record:
            out["_browser_vs_vendor"] = {v: {"passed": p, "failed": f} for v, (p, f) in self.vendor_record.items()}
        return out
