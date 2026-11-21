"""News crawler: feed -> freshness gate -> article page -> full text -> NewsRecord.

Per source:
  1. fetch the RSS/Atom feed; every entry becomes a ListingItem whose date is
     the feed's <pubDate>/<published> (never <updated>)
  2. gate.screen_listing: STALE / DUPLICATE entries stop here (no page fetch)
  3. fetch each remaining article page concurrently (per-host limits apply),
     gate.screen_page re-derives the date from the page itself (JSON-LD,
     meta tags …) – the page wins over the feed when it is more reliable
  4. trafilatura full text; gate.finalize drops syndicated near-copies
  5. NewsRecord with date provenance; source URL = the article URL

Protected publishers: with a ProtectedFetcher, article pages go through
robots.txt + bot-protection handling (core/protected.py). If a page is behind
a captcha / hard block (Ars Technica's AWS WAF), the item is kept ONLY when the
feed date alone already proves it fresh, and its text is the publisher's own
feed body, labelled text_source="feed-lead" so nobody mistakes it for full text.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from ...core.http import FetchError, HttpFetcher
from ...core.protected import Blocked, ProtectedFetcher
from ...core.logging import get_logger
from ...freshness import FreshnessGate, ListingItem, Verdict
from ...freshness.date_parser import DateResult, parse_date, tz_from_name
from ...freshness.gate import Decision, InOrder
from ...schemas import NewsContent, NewsRecord, Source
from ..signal_stats import SourceStats
from ..text import extract_article_async, strip_html
from .feeds import FeedItem, parse_feed
from .sources import NewsSource

log = get_logger(__name__)
UTC = timezone.utc


class NewsCrawler:
    MIN_FEED_LEAD_WORDS = 60

    def __init__(self, http: HttpFetcher, gate: FreshnessGate, fetcher: ProtectedFetcher | None = None):
        self.http = http
        self.gate = gate
        self.fetcher = fetcher

    async def crawl_source(self, src: NewsSource, now: datetime | None = None) -> tuple[list[NewsRecord], SourceStats]:
        now = now or datetime.now(UTC)
        stats = SourceStats(src.name)
        run = self.gate.begin(src.name, now)
        ok = False
        records: list[NewsRecord] = []
        try:
            xml = await self.http.get_text(src.feed_url)
            items = parse_feed(xml)
            stats.listed = len(items)
            tz = tz_from_name(src.timezone)
            listing = [ListingItem(it.link, date=self._feed_date(it, now, tz)) for it in items]
            decisions = self.gate.screen_listing(run, listing, now)
            by_url = {it.link: it for it in items}

            todo = []
            for d in decisions:
                if d.verdict is Verdict.STALE:
                    stats.stale += 1
                elif d.verdict is Verdict.DUPLICATE:
                    stats.duplicate += 1
                else:
                    todo.append(d)
            order = InOrder(len(todo))

            async def ordered(i: int, d: Decision):
                try:
                    return await self._process(d, by_url[d.url], src, now, stats, turn=lambda: order.turn(i))
                finally:
                    await order.done(i)
            results = await asyncio.gather(*(ordered(i, d) for i, d in enumerate(todo)))
            records = [r for r in results if r is not None]
            ok = True
        except (FetchError, ValueError, SyntaxError) as exc:   # ET.ParseError is a SyntaxError
            stats.errors.append(f"feed: {exc}")
            log.error("news.feed_failed", source=src.name, error=str(exc))
        finally:
            # a failed run must not become the baseline for "new since last run"
            self.gate.end(run, ok=ok)
        stats.stored = len(records)
        log.info("news.source_done", **{k: v for k, v in stats.as_dict().items() if k != "errors"})
        return records, stats

    @staticmethod
    def _feed_date(it: FeedItem, now: datetime, tz) -> DateResult | None:
        p = parse_date(it.published_raw, now, default_tz=tz) if it.published_raw else None
        if not p:
            return None
        return DateResult(p.dt, "rss:pubDate", 0.9, it.published_raw[:80], p.date_only, p.relative, p.granularity_s)

    async def _process(self, d: Decision, item: FeedItem, src: NewsSource, now: datetime,
                       stats: SourceStats, turn=None) -> NewsRecord | None:
        try:
            if self.fetcher is not None:
                page = await self.fetcher.fetch(d.url)
                html, headers, final_url = page.html, page.headers, page.final_url
            else:
                resp = await self.http.get(d.url)
                html, headers, final_url = resp.text, dict(resp.headers), str(resp.url)
        except Blocked as exc:
            if exc.block.kind.terminal:
                return await self._from_feed(d, item, src, stats, turn, exc)
            self.gate.release(d)
            stats.errors.append(f"{d.url}: blocked {exc.block.kind.value}")
            return None
        except FetchError as exc:
            self.gate.release(d)          # transient: let a later run retry it
            stats.errors.append(f"{d.url}: {exc.status}")
            return None
        self.gate.screen_page(d, html, headers, now)
        if d.verdict is not Verdict.FRESH:
            stats.stale += d.verdict is Verdict.STALE
            stats.uncertain += d.verdict is Verdict.UNCERTAIN
            self.gate.finalize(d)         # records the rejection
            return None

        art = await extract_article_async(html, final_url)
        if turn:
            await turn()                  # near-dup claims happen in feed order
        if not self.gate.finalize(d, art.text):
            stats.near_duplicate += 1
            return None
        stats.fresh += 1
        summary = strip_html(item.summary_html, limit=600) or (art.description or None)
        return NewsRecord(
            source=Source(name=src.label, url=final_url),
            content=NewsContent(
                title=item.title or art.title or "",
                published_date=d.date.dt,
                date_source=d.date.source,
                date_confidence=d.date.confidence,
                author=item.author or art.author,
                summary=summary,
                full_text=art.text,
                text_source="article-page",
                word_count=len(art.text.split()),
            ),
        )

    async def _from_feed(self, d: Decision, item: FeedItem, src: NewsSource, stats: SourceStats,
                         turn, exc: Blocked) -> NewsRecord | None:
        """Article page is behind a captcha / hard block: fall back to the publisher's feed."""
        stats.blocked += 1
        lead = strip_html(item.content_html or item.summary_html or "", limit=20000)
        if d.verdict is not Verdict.FRESH or d.needs_page or not d.date or len(lead.split()) < self.MIN_FEED_LEAD_WORDS:
            # the page was needed to prove freshness, or the feed has too little text
            self.gate.reject(d, f"page blocked ({exc.block.kind.value}), feed insufficient")
            return None
        lead = lead.removesuffix("Read full article Comments").removesuffix("Read full article").strip()
        if turn:
            await turn()
        if not self.gate.finalize(d, lead):
            stats.near_duplicate += 1
            return None
        stats.fresh += 1
        return NewsRecord(
            source=Source(name=src.label, url=item.link),
            content=NewsContent(
                title=item.title, published_date=d.date.dt, date_source=d.date.source,
                date_confidence=d.date.confidence, author=item.author,
                summary=strip_html(item.summary_html, limit=600) or None,
                full_text=lead, text_source="feed-lead", word_count=len(lead.split()),
            ),
        )
