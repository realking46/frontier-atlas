"""Browser tier: render a page in real headless Chromium (Playwright, async).

Used only when the plain HTTP tier got a JS challenge, an empty JS app shell,
or a bare WAF denial. What it does:

  * runs the page's JavaScript, so client-rendered pages and NON-interactive
    challenges (Cloudflare "Just a moment", which completes by itself in a real
    browser) resolve the way they do for any visitor;
  * keeps one browser context per site, so cookies a site sets (including a
    passed challenge's clearance cookie) are reused instead of re-challenging;
  * blocks images / media / fonts, which cuts bandwidth ~80% per page;
  * bounds concurrency (a browser page costs ~50-100 MB).

What it deliberately does NOT do: no clicking, no captcha solving or
captcha-solving services, no fingerprint spoofing / "stealth" patches, no
proxy rotation. If the page still asks for a human after the wait, the result
is reported as a block and the caller routes to another channel.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

from .antibot import Block, BlockKind, classify
from .logging import get_logger

log = get_logger(__name__)
_SKIP_RESOURCES = {"image", "media", "font"}


@dataclass
class Rendered:
    url: str
    status: int
    html: str
    headers: dict[str, str] = field(default_factory=dict)
    block: Block = field(default_factory=lambda: Block(BlockKind.NONE))
    seconds: float = 0.0


class BrowserRenderer:
    def __init__(self, *, max_pages: int = 3, nav_timeout_s: float = 30, challenge_wait_s: float = 15,
                 headless: bool = True):
        self.max_pages = max_pages
        self.nav_timeout_ms = int(nav_timeout_s * 1000)
        self.challenge_wait_s = challenge_wait_s
        self.headless = headless
        self._sem = asyncio.Semaphore(max_pages)
        self._pw = None
        self._browser = None
        self._contexts: dict[str, object] = {}
        self._start_lock = asyncio.Lock()
        self.renders = 0

    @staticmethod
    def available() -> bool:
        try:
            import playwright.async_api  # noqa: F401
        except ImportError:
            return False
        return True

    async def _ensure(self) -> None:
        async with self._start_lock:
            if self._browser is None:
                from playwright.async_api import async_playwright

                self._pw = await async_playwright().start()
                self._browser = await self._pw.chromium.launch(headless=self.headless)
                log.info("browser.started", headless=self.headless)

    async def _context(self, url: str):
        site = urlparse(url).netloc.lower()
        if site not in self._contexts:
            ctx = await self._browser.new_context(locale="en-US", viewport={"width": 1366, "height": 900})

            async def _route(route):
                if route.request.resource_type in _SKIP_RESOURCES:
                    await route.abort()
                else:
                    await route.continue_()
            await ctx.route("**/*", _route)
            self._contexts[site] = ctx
        return self._contexts[site]

    async def render(self, url: str) -> Rendered:
        await self._ensure()
        t0 = time.monotonic()
        async with self._sem:
            ctx = await self._context(url)
            page = await ctx.new_page()
            last_doc: dict = {}

            def on_response(resp):
                if resp.request.resource_type == "document" and resp.frame == page.main_frame:
                    last_doc["status"], last_doc["headers"], last_doc["url"] = resp.status, resp.headers, resp.url
            page.on("response", on_response)
            try:
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=self.nav_timeout_ms)
                if resp is not None:
                    last_doc.setdefault("status", resp.status)
                    last_doc.setdefault("headers", await resp.all_headers())
                deadline = time.monotonic() + self.challenge_wait_s
                rendered_shell = False
                while True:
                    html = await page.content()
                    block = classify(last_doc.get("status", 0), last_doc.get("headers", {}), html)
                    if block.kind is BlockKind.JS_CHALLENGE and time.monotonic() < deadline:
                        # a non-interactive challenge finishes by itself and reloads the page
                        await page.wait_for_timeout(1000)
                        continue
                    if block.kind is BlockKind.JS_REQUIRED and not rendered_shell:
                        rendered_shell = True
                        try:
                            await page.wait_for_load_state("networkidle", timeout=10000)
                        except Exception:
                            pass
                        continue
                    break
                self.renders += 1
                return Rendered(url=page.url, status=last_doc.get("status", 0), html=html,
                                headers=dict(last_doc.get("headers", {})), block=block,
                                seconds=round(time.monotonic() - t0, 2))
            finally:
                await page.close()

    async def close(self) -> None:
        for ctx in self._contexts.values():
            await ctx.close()
        self._contexts.clear()
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()
        self._browser = self._pw = None

    async def __aenter__(self) -> "BrowserRenderer":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()
