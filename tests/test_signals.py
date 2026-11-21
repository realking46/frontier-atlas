from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from frontier_atlas.core.http import HttpFetcher
from frontier_atlas.crawlers.jobs.boards import JobBoard, JobPosting, parse_hn_listing
from frontier_atlas.crawlers.jobs.classify import ai_relevance, infer_remote, role_family
from frontier_atlas.crawlers.jobs.crawler import JobCrawler
from frontier_atlas.crawlers.news.crawler import NewsCrawler
from frontier_atlas.crawlers.news.feeds import parse_feed
from frontier_atlas.crawlers.news.sources import NewsSource
from frontier_atlas.freshness import FreshnessGate, SeenStore
from frontier_atlas.freshness.date_parser import DateResult

from .conftest import make_settings

UTC = timezone.utc
NOW = datetime.now(UTC).replace(microsecond=0)


def rfc(dt: datetime) -> str:
    return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")


# ---------------------------------------------------------------- feeds
def test_parse_rss_and_atom_never_uses_updated():
    rss = f"""<rss><channel><item><title>A</title><link>https://n.example/a</link>
      <pubDate>{rfc(NOW)}</pubDate><dc:creator xmlns:dc="http://purl.org/dc/elements/1.1/">Ann</dc:creator>
      <description>&lt;p&gt;Dek&lt;/p&gt;</description></item></channel></rss>"""
    it = parse_feed(rss)[0]
    assert (it.link, it.title, it.author) == ("https://n.example/a", "A", "Ann")
    atom = """<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>B</title>
      <updated>2026-11-20T09:36:42-05:00</updated><published>2026-11-20T07:30:47-05:00</published>
      <link rel="alternate" href="https://v.example/b"/><author><name>Bo</name></author></entry></feed>"""
    it = parse_feed(atom)[0]
    assert it.published_raw == "2026-11-20T07:30:47-05:00" and it.author == "Bo" and it.link == "https://v.example/b"


# ---------------------------------------------------------------- classification
@pytest.mark.parametrize("title,tags,desc,expected", [
    ("Senior ML Engineer", [], "", True),
    ("Machine Learning Researcher", [], "", True),
    ("Backend Engineer", ["LLM", "Python"], "", True),
    ("Backend Engineer", [], "We build RAG pipelines and fine-tune LLMs in PyTorch.", True),
    ("Web3 Business Development", ["crypto"], "", False),
    ("Account Executive", [], "Join our AI-powered sales platform.", False),   # one boilerplate hit
    ("Office Manager", [], "He said the office is nice.", False),             # 'said' must not match AI
    # seen live: generic AI boilerplate + one technical term is not enough
    ("Support Engineer", [], "Our AI platform lets AI agents review code.", False),
    ("Interventional Cardiologist", [], "Help train AI with reinforcement learning feedback.", False),
    ("GRC Lead", [], "We use machine learning and AI agents.", False),
    ("Full Stack Software Engineer", [], "Work with data scientists shipping machine learning products.", True),
])
def test_ai_relevance(title, tags, desc, expected):
    assert ai_relevance(title, tags, desc)[0] is expected


@pytest.mark.parametrize("title,family", [
    ("Senior Machine Learning Engineer", "Engineering"),
    ("Research Scientist, Alignment", "Research"),
    ("Data Scientist", "Data"),
    ("Product Manager, AI Platform", "Product"),
    ("Solutions Engineer", "Sales"),
    ("Product Designer", "Design"),
    ("Growth Marketing Lead", "Marketing"),
    ("Recruiting Coordinator", "Operations"),
    ("Founding GTM Lead", "Other"),
    ("Software Sales Associate Director", "Sales"),
    ("Director, Pre-Sales, EMEA", "Sales"),
    ("Salesforce Developer", "Engineering"),
    ("Member of Technical Staff - Safety", "Engineering"),
    ("Desenvolvedor Python Bilíngue", "Engineering"),
    ("Analytical Evaluator - AI Feedback - AI Trainer", "Data"),
])
def test_role_family(title, family):
    assert role_family(title) == family


def test_infer_remote():
    assert infer_remote("Remote (US)") is True
    assert infer_remote("San Francisco, on-site") is False
    assert infer_remote("Berlin") is None


# ---------------------------------------------------------------- HN listing (relative dates)
def test_parse_hn_listing():
    t = (NOW - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S")
    html = f"""<table><tr class="athing" id="1"><td><span class="titleline">
      <a href="https://www.ycombinator.com/companies/acme/jobs/x">Acme (YC W24) Is Hiring a Founding ML Engineer</a></span></td></tr>
      <tr><td class="subtext"><span class="age" title="{t} 1795003216"><a href="item?id=1">3 hours ago</a></span></td></tr>
      <tr class="athing" id="2"><td><span class="titleline"><a href="item?id=2">Beta (YC S21) is hiring</a></span></td></tr>
      <tr><td class="subtext"><span class="age"><a href="item?id=2">2 days ago</a></span></td></tr></table>"""
    a, b = parse_hn_listing(html, NOW)
    assert (a.company, a.title, a.tags) == ("Acme", "Founding ML Engineer", ["YC W24"])
    assert a.date.source == "listing:age-title" and a.date.dt == NOW - timedelta(hours=3)
    assert b.url == "https://news.ycombinator.com/item?id=2"
    assert b.date.source == "listing:relative" and b.date.dt == NOW - timedelta(days=2)


# ---------------------------------------------------------------- news crawler end-to-end (mocked network)
ARTICLE = """<html><head><script type="application/ld+json">{{"@type":"NewsArticle","datePublished":"{d}"}}</script>
</head><body><article><h1>{t}</h1>{body}</article></body></html>"""


def _body(seed: str) -> str:
    return "".join(f"<p>{seed} paragraph {i} about frontier models, chips and funding rounds in the AI market.</p>"
                   for i in range(25))


def _mock_news():
    fresh = NOW - timedelta(hours=2)
    stale = NOW - timedelta(days=3)
    feed = f"""<rss><channel>
      <item><title>Fresh story</title><link>https://n.example/fresh?utm_source=rss</link><pubDate>{rfc(fresh)}</pubDate></item>
      <item><title>Old story</title><link>https://n.example/old</link><pubDate>{rfc(stale)}</pubDate></item>
      <item><title>Feed lies</title><link>https://n.example/backdated</link><pubDate>{rfc(fresh)}</pubDate></item>
      <item><title>Copy</title><link>https://n.example/copy</link><pubDate>{rfc(fresh)}</pubDate></item>
    </channel></rss>"""
    pages = {
        "/fresh": ARTICLE.format(d=fresh.isoformat(), t="Fresh story", body=_body("alpha")),
        "/backdated": ARTICLE.format(d=stale.isoformat(), t="Feed lies", body=_body("beta")),
        "/copy": ARTICLE.format(d=fresh.isoformat(), t="Copy", body=_body("alpha")),
    }
    calls: list[str] = []

    def handler(req: httpx.Request):
        calls.append(req.url.path)
        if req.url.path == "/feed":
            return httpx.Response(200, text=feed)
        if req.url.path in pages:
            return httpx.Response(200, text=pages[req.url.path], headers={"content-type": "text/html"})
        return httpx.Response(404)

    return handler, calls


async def test_news_crawler_end_to_end():
    handler, calls = _mock_news()
    gate = FreshnessGate(SeenStore(":memory:"))
    src = NewsSource("test-news", "Test News", "https://n.example/feed")
    async with HttpFetcher(make_settings(), client=httpx.AsyncClient(transport=httpx.MockTransport(handler))) as http:
        records, stats = await NewsCrawler(http, gate).crawl_source(src, NOW)

        assert [r.content.title for r in records] == ["Fresh story"]
        rec = records[0]
        assert rec.content.date_source == "jsonld:datePublished"      # page date wins over feed
        assert rec.content.word_count > 200 and "frontier models" in rec.content.full_text
        assert str(rec.source.url).startswith("https://n.example/fresh")
        assert stats.stale == 2              # old (from feed) + backdated (caught on the page)
        assert stats.near_duplicate == 1     # /copy has the same text as /fresh
        assert "/old" not in calls           # stale feed entries are never fetched

        # second run: nothing is processed twice, nothing is re-fetched
        calls.clear()
        records2, stats2 = await NewsCrawler(http, gate).crawl_source(src, NOW + timedelta(minutes=30))
        assert records2 == [] and stats2.duplicate == 3
        assert calls == ["/feed"]


# ---------------------------------------------------------------- job crawler (fake board)
class FakeBoard(JobBoard):
    name, label = "fake", "Fake Board"

    def __init__(self, pages):
        self._pages = pages
        self.served = 0

    async def pages(self, http, now):
        for p in self._pages:
            self.served += 1
            yield p


def _post(url, title, hours_ago, desc="", company="Acme"):
    d = DateResult(NOW - timedelta(hours=hours_ago), "api", 0.95, "")
    return JobPosting("fake", url, title, company, d, location="Remote", remote=True, description=desc)


async def test_job_crawler_filters_and_stops_paging():
    long_desc = " ".join(f"word{i}" for i in range(200)) + " We train LLMs with PyTorch."
    pages = [
        [_post("https://j.example/1", "Senior ML Engineer", 1, long_desc),
         _post("https://j.example/2", "Office Manager", 2, "Nice office."),
         _post("https://k.example/1-crosspost", "Senior ML Engineer", 3, long_desc)],
        [_post("https://j.example/3", "Data Scientist", 30)],          # whole page stale -> stop
        [_post("https://j.example/4", "AI Engineer", 1)],               # must never be requested
    ]
    board = FakeBoard(pages)
    gate = FreshnessGate(SeenStore(":memory:"))
    async with HttpFetcher(make_settings(), client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404)))) as http:
        records, stats = await JobCrawler(http, gate).crawl_board(board, NOW)
    assert [r.content.title for r in records] == ["Senior ML Engineer"]
    r = records[0].content
    assert r.role_family == "Engineering" and r.is_remote is True and "ML" in r.ai_keywords
    assert stats.not_ai == 1 and stats.near_duplicate == 1 and stats.stale == 1
    assert board.served == 2


# ---------------------------------------------------------------- protected publisher: page behind captcha
async def test_news_blocked_page_falls_back_to_feed_lead():
    from frontier_atlas.core.protected import ProtectedFetcher

    fresh = NOW - timedelta(hours=3)
    lead = " ".join(f"word{i}" for i in range(120))
    feed = f"""<rss xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel>
      <item><title>Blocked story</title><link>https://p.example/a</link><pubDate>{rfc(fresh)}</pubDate>
        <description>Short dek.</description>
        <content:encoded><![CDATA[<p>{lead}</p><a href="https://p.example/a">Read full article</a> <a>Comments</a>]]></content:encoded></item>
      <item><title>Undated</title><link>https://p.example/b</link>
        <content:encoded><![CDATA[<p>{lead}</p>]]></content:encoded></item>
    </channel></rss>"""

    def handler(req: httpx.Request):
        if req.url.path == "/feed":
            return httpx.Response(200, text=feed)
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(405, text="<title>Human Verification</title>",
                              headers={"x-amzn-waf-action": "captcha"})

    gate = FreshnessGate(SeenStore(":memory:"))
    src = NewsSource("prot", "Protected News", "https://p.example/feed")
    async with HttpFetcher(make_settings(http_max_retries=0),
                           client=httpx.AsyncClient(transport=httpx.MockTransport(handler))) as http:
        pf = ProtectedFetcher(http, None)
        records, stats = await NewsCrawler(http, gate, pf).crawl_source(src, NOW)
    assert [r.content.title for r in records] == ["Blocked story"]
    c = records[0].content
    assert c.text_source == "feed-lead" and c.date_source == "rss:pubDate"
    assert c.full_text.startswith("word0") and "Read full article" not in c.full_text
    assert stats.blocked >= 1           # the undated item needed its page, so it was not ingested


def test_media_content_does_not_hide_content_encoded():
    feed = """<rss xmlns:content="http://purl.org/rss/1.0/modules/content/" xmlns:media="http://search.yahoo.com/mrss/"><channel>
      <item><title>T</title><link>https://x.example/a</link>
        <content:encoded><![CDATA[<p>The body text.</p>]]></content:encoded>
        <media:content url="https://x.example/i.jpg" medium="image">
        </media:content></item></channel></rss>"""
    [it] = parse_feed(feed)
    assert "The body text." in it.content_html
