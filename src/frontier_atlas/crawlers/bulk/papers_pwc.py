"""paperswithcode.co – community successor of Papers with Code (the site in the brief).

Discovery: the sitemap index lists ~85 paper sitemaps (thousands of URLs each),
so the whole catalogue is enumerable without any search UI.
Extraction: pages are server-rendered with schema.org JSON-LD (title, authors,
abstract) – no headless browser or LLM needed. GitHub links are pulled from the
HTML and disambiguated with `choose_repo` because pages also link to generic
frameworks (vLLM, transformers …).
Politeness: robots.txt allows generic crawlers; we keep 1 request/s.
"""
from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from datetime import datetime

from selectolax.lexbor import LexborHTMLParser

from ...core.http import FetchError, HttpFetcher
from ...core.logging import get_logger
from .github_links import choose_repo, find_repos, repo_url
from .paper_types import PaperCandidate, arxiv_id_from

log = get_logger(__name__)
BASE = "https://paperswithcode.co"
LOC_RE = re.compile(r"<loc>([^<]+)</loc>")


async def paper_sitemaps(http: HttpFetcher) -> list[str]:
    text = await http.get_text(f"{BASE}/sitemap.xml")
    maps = [u for u in LOC_RE.findall(text) if "/sitemaps/papers-" in u]
    return sorted(maps, reverse=True)  # newest years first


async def iter_paper_urls(http: HttpFetcher) -> AsyncIterator[str]:
    for sm in await paper_sitemaps(http):
        try:
            text = await http.get_text(sm)
        except FetchError as exc:
            log.warning("pwc.sitemap_failed", url=sm, error=str(exc))
            continue
        for url in LOC_RE.findall(text):
            yield url


def parse_paper_page(html: str, url: str) -> PaperCandidate | None:
    tree = LexborHTMLParser(html)
    article = None
    for node in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.text() or "")
        except ValueError:
            continue
        for item in data.get("@graph", [data]) if isinstance(data, dict) else []:
            if isinstance(item, dict) and item.get("@type") == "ScholarlyArticle":
                article = item
                break
    if not article or not article.get("headline"):
        return None

    authors = [a.get("name", "").strip() for a in article.get("author") or [] if isinstance(a, dict)]
    arxiv_links = re.findall(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})", html)
    aid = arxiv_id_from(arxiv_links[0]) if arxiv_links else arxiv_id_from(url.rsplit("/", 1)[-1])
    repos = find_repos(html)
    title = " ".join(article["headline"].split())
    chosen = choose_repo(repos, title)
    published = article.get("datePublished")
    return PaperCandidate(
        source_name="paperswithcode",
        source_url=url,
        title=title,
        authors=[a for a in authors if a],
        arxiv_id=aid,
        abstract=article.get("abstract"),
        published=datetime.fromisoformat(published.replace("Z", "+00:00")) if published else None,
        paper_url=f"https://arxiv.org/abs/{aid}" if aid else url,
        github_url=repo_url(chosen) if chosen else None,
        repo_candidates=repos,
    )


async def fetch_paper(http: HttpFetcher, url: str) -> PaperCandidate | None:
    try:
        html = await http.get_text(url)
    except FetchError as exc:
        log.warning("pwc.page_failed", url=url, error=str(exc))
        return None
    return parse_paper_page(html, url)
