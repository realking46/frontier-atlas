"""Monitored AI news sources (5 + 1 protected showcase). Adding a source = one line here."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class NewsSource:
    name: str        # stable id, used for run history / freshness heuristics
    label: str       # human-readable source.name in records
    feed_url: str
    timezone: str | None = None   # for sources that publish naive local times


NEWS_SOURCES: tuple[NewsSource, ...] = (
    NewsSource("techcrunch-ai", "TechCrunch", "https://techcrunch.com/category/artificial-intelligence/feed/"),
    NewsSource("theverge-ai", "The Verge", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),
    NewsSource("mittr-ai", "MIT Technology Review", "https://www.technologyreview.com/topic/artificial-intelligence/feed"),
    NewsSource("the-decoder", "The Decoder", "https://the-decoder.com/feed/"),
    NewsSource("wired-ai", "WIRED", "https://www.wired.com/feed/tag/ai/latest/rss"),
    # Protected publisher (article pages behind an AWS WAF captcha). Monitored as a 6th,
    # showcase source: items come with the feed lead only, labelled text_source="feed-lead".
    NewsSource("arstechnica-ai", "Ars Technica", "https://arstechnica.com/ai/feed/"),
)
