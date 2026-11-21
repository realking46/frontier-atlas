from __future__ import annotations

import httpx
import pytest

from frontier_atlas.core.antibot import BlockKind, classify
from frontier_atlas.core.browser import Rendered
from frontier_atlas.core.http import FetchError, HttpFetcher
from frontier_atlas.core.protected import Blocked, ProtectedFetcher
from frontier_atlas.core.robots import RobotsCache

from .conftest import make_settings

CF_CHALLENGE = "<html><head><title>Just a moment...</title></head><body><script src='/cdn-cgi/challenge-platform/x'></script></body></html>"
CF_BLOCK = "<html><head><title>Attention Required! | Cloudflare</title></head><body>Sorry, you have been blocked</body></html>"
DATADOME = "<html><head><title>g2.com</title></head><body><script src='https://ct.captcha-delivery.com/c.js'></script></body></html>"
AWS_CAPTCHA = "<html><head><title>Human Verification</title></head><body></body></html>"
SHELL = "<html><head><title>App</title></head><body><noscript>You need to enable JavaScript to run this app.</noscript><div id=\"root\"></div></body></html>"
REAL = "<html><head><title>Acme</title></head><body>" + "<p>Real article text. </p>" * 40 + "</body></html>"


# ---------------------------------------------------------------- classification (signatures seen live)
@pytest.mark.parametrize("status,headers,text,kind,vendor", [
    (403, {"cf-mitigated": "challenge", "server": "cloudflare"}, CF_CHALLENGE, BlockKind.JS_CHALLENGE, "cloudflare"),
    (403, {"server": "cloudflare"}, CF_BLOCK, BlockKind.HARD_BLOCK, "cloudflare"),
    (403, {"x-datadome": "protected", "server": "cloudflare"}, DATADOME, BlockKind.CAPTCHA, "datadome"),
    (405, {"x-amzn-waf-action": "captcha", "server": "awselb/2.0"}, AWS_CAPTCHA, BlockKind.CAPTCHA, "aws-waf"),
    (403, {"server": "nginx"}, "<html><title>foo</title></html>", BlockKind.WAF_DENIED, "nginx"),
    (429, {}, "", BlockKind.RATE_LIMITED, ""),
    (503, {}, "overloaded", BlockKind.RATE_LIMITED, ""),
    (200, {}, SHELL, BlockKind.JS_REQUIRED, ""),
    (200, {}, REAL, BlockKind.NONE, ""),
    # 200 with real content that merely embeds a Turnstile widget (wellfound) is NOT a block
    (200, {"server": "cloudflare"}, REAL.replace("</body>", "<script src='/cdn-cgi/challenge-platform/t'></script>"
                                                               "<div class='cf-turnstile'></div></body>"), BlockKind.NONE, ""),
])
def test_classify(status, headers, text, kind, vendor):
    b = classify(status, headers, text)
    assert b.kind is kind and (not vendor or b.vendor == vendor)


def test_kind_policy():
    assert BlockKind.JS_CHALLENGE.browser_may_help and BlockKind.WAF_DENIED.browser_may_help
    assert not BlockKind.CAPTCHA.browser_may_help and BlockKind.CAPTCHA.terminal
    assert BlockKind.HARD_BLOCK.terminal and BlockKind.ROBOTS.terminal


# ---------------------------------------------------------------- tiered fetcher
class FakeBrowser:
    """Stands in for Playwright: `pages` maps url -> (status, html, headers)."""

    def __init__(self, pages):
        self.pages = pages
        self.calls: list[str] = []

    async def render(self, url):
        self.calls.append(url)
        status, html, headers = self.pages[url]
        return Rendered(url, status, html, headers, classify(status, headers, html))

    async def close(self):
        pass


def _http(routes: dict, calls: list | None = None) -> HttpFetcher:
    def handler(req: httpx.Request):
        if calls is not None:
            calls.append(str(req.url))
        if req.url.path == "/robots.txt":
            body = routes.get(f"{req.url.scheme}://{req.url.host}/robots.txt")
            return httpx.Response(200, text=body) if body else httpx.Response(404)
        status, html, headers = routes[str(req.url)]
        return httpx.Response(status, text=html, headers=headers)
    return HttpFetcher(make_settings(http_max_retries=0), client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_plain_page_served_by_http_tier():
    async with _http({"https://a.example/x": (200, REAL, {})}) as http:
        pf = ProtectedFetcher(http, FakeBrowser({}))
        page = await pf.fetch("https://a.example/x")
    assert page.tier == "http" and "Real article" in page.html


async def test_js_challenge_escalates_and_host_stays_on_browser():
    calls = []
    routes = {u: (403, CF_CHALLENGE, {"cf-mitigated": "challenge", "server": "cloudflare"})
              for u in ("https://c.example/1", "https://c.example/2")}
    br = FakeBrowser({u: (200, REAL, {}) for u in routes})
    async with _http(routes, calls) as http:
        pf = ProtectedFetcher(http, br)
        p1 = await pf.fetch("https://c.example/1")
        p2 = await pf.fetch("https://c.example/2")
    assert (p1.tier, p2.tier) == ("browser", "browser")
    assert "https://c.example/2" not in calls          # sticky tier: no wasted HTTP attempt
    assert pf.stats["c.example"]["escalated:js_challenge"] == 1 and pf.stats["c.example"]["ok:browser"] == 2


async def test_captcha_never_escalates_and_opens_circuit():
    routes = {u: (403, DATADOME, {"x-datadome": "protected"}) for u in ("https://g.example/1", "https://g.example/2")}
    br = FakeBrowser({})
    calls = []
    async with _http(routes, calls) as http:
        pf = ProtectedFetcher(http, br)
        with pytest.raises(Blocked) as e1:
            await pf.fetch("https://g.example/1")
        with pytest.raises(Blocked) as e2:
            await pf.fetch("https://g.example/2")
    assert e1.value.block.kind is BlockKind.CAPTCHA and br.calls == []      # captcha: browser never tried
    assert "circuit open" in str(e2.value) and "https://g.example/2" not in calls
    assert http._host_intervals["g.example"] >= 2.0                         # host slowed down


async def test_browser_still_challenged_counts_toward_breaker():
    urls = [f"https://t.example/{i}" for i in range(4)]
    ch = (403, CF_CHALLENGE, {"cf-mitigated": "challenge"})
    async with _http({u: ch for u in urls}) as http:
        pf = ProtectedFetcher(http, FakeBrowser({u: ch for u in urls}), breaker_threshold=2)
        for u in urls[:2]:
            with pytest.raises(Blocked):
                await pf.fetch(u)
        with pytest.raises(Blocked, match="circuit open"):
            await pf.fetch(urls[2])


async def test_robots_disallow_and_crawl_delay():
    robots = "User-agent: *\nDisallow: /private\nCrawl-delay: 4\n"
    routes = {"https://r.example/robots.txt": robots, "https://r.example/ok": (200, REAL, {})}
    async with _http(routes) as http:
        pf = ProtectedFetcher(http, None)
        with pytest.raises(Blocked) as e:
            await pf.fetch("https://r.example/private/page")
        assert e.value.block.kind is BlockKind.ROBOTS
        await pf.fetch("https://r.example/ok")
        assert http._host_intervals["r.example"] == 4.0


async def test_not_found_is_an_error_not_a_block():
    async with _http({"https://n.example/x": (404, "<title>Not found</title>", {})}) as http:
        pf = ProtectedFetcher(http, None)
        with pytest.raises(FetchError) as e:
            await pf.fetch("https://n.example/x")
    assert not isinstance(e.value, Blocked) and e.value.status == 404
    assert pf.host_state("n.example").consecutive_blocks == 0


async def test_robots_unreachable_means_allowed():
    def handler(req):
        if req.url.path == "/robots.txt":
            raise httpx.ConnectError("down")
        return httpx.Response(200, text=REAL)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        rc = RobotsCache(client, "FrontierAtlasBot")
        assert await rc.allowed("https://u.example/a")


async def test_probe_first_hits_a_captcha_wall_once():
    import asyncio
    urls = [f"https://w.example/{i}" for i in range(6)]
    calls = []
    async with _http({u: (405, "<title>Human Verification</title>", {"x-amzn-waf-action": "captcha"}) for u in urls},
                     calls) as http:
        pf = ProtectedFetcher(http, None)
        results = await asyncio.gather(*(pf.fetch(u) for u in urls), return_exceptions=True)
    assert all(isinstance(r, Blocked) for r in results)
    assert len([c for c in calls if "robots" not in c]) == 1      # one request reached the wall
    assert pf.stats["w.example"]["skipped:circuit_open"] == 5


async def test_vendor_learning_stops_hopeless_escalations():
    ch = (403, CF_CHALLENGE, {"cf-mitigated": "challenge", "server": "cloudflare"})
    urls = [f"https://h{i}.example/" for i in range(8)]          # 8 different hosts, same vendor
    br = FakeBrowser({u: ch for u in urls})
    async with _http({u: ch for u in urls}) as http:
        pf = ProtectedFetcher(http, br, vendor_giveup_after=3)
        for u in urls:
            with pytest.raises(Blocked):
                await pf.fetch(u)
    assert len(br.calls) == 3                                    # escalations stop after 3 failures
    assert pf.vendor_record["cloudflare"] == [0, 3]
    assert pf.summary()["_browser_vs_vendor"]["cloudflare"] == {"passed": 0, "failed": 3}
