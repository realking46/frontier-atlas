"""arXiv export API – the breadth source (hundreds of thousands of AI papers).

Paging: `start`/`max_results` over a category query sorted by submission
date. arXiv asks for at most one request every 3 s, which HttpFetcher enforces
for export.arxiv.org. For >10k results arXiv recommends slicing by date
range, so `iter_window` walks one date window at a time – the same code reaches
500k records by just running more windows (or more workers on disjoint windows).
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

from ...core.http import FetchError, HttpFetcher
from ...core.logging import get_logger
from .github_links import find_repos, repo_url
from .paper_types import PaperCandidate, arxiv_id_from

log = get_logger(__name__)
API = "https://export.arxiv.org/api/query"
NS = {
    "a": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
    "os": "http://a9.com/-/spec/opensearch/1.1/",
}
AI_CATEGORIES = ("cs.AI", "cs.LG", "cs.CL", "cs.CV", "cs.MA", "cs.NE", "cs.IR", "stat.ML")


def _text(el: ET.Element | None) -> str:
    return " ".join((el.text or "").split()) if el is not None else ""


def parse_feed(xml_text: str) -> tuple[int, list[PaperCandidate]]:
    root = ET.fromstring(xml_text)
    total = int(_text(root.find("os:totalResults", NS)) or 0)
    out: list[PaperCandidate] = []
    for e in root.findall("a:entry", NS):
        abs_url = _text(e.find("a:id", NS))
        aid = arxiv_id_from(abs_url)
        title = _text(e.find("a:title", NS))
        if not aid or not title:
            continue
        abstract = _text(e.find("a:summary", NS))
        comment = _text(e.find("arxiv:comment", NS))
        repos = find_repos(f"{abstract} {comment}")
        published = _text(e.find("a:published", NS))
        out.append(
            PaperCandidate(
                source_name="arxiv",
                source_url=f"https://arxiv.org/abs/{aid}",
                title=title,
                authors=[_text(a.find("a:name", NS)) for a in e.findall("a:author", NS)],
                arxiv_id=aid,
                abstract=abstract or None,
                published=datetime.fromisoformat(published.replace("Z", "+00:00")) if published else None,
                paper_url=f"https://arxiv.org/abs/{aid}",
                github_url=repo_url(repos[0]) if repos else None,
                repo_candidates=repos,
            )
        )
    return total, out


def build_query(categories=AI_CATEGORIES, start: datetime | None = None, end: datetime | None = None) -> str:
    q = "(" + " OR ".join(f"cat:{c}" for c in categories) + ")"
    if start and end:
        q += f" AND submittedDate:[{start:%Y%m%d%H%M} TO {end:%Y%m%d%H%M}]"
    return q


async def iter_window(
    http: HttpFetcher,
    start: datetime,
    end: datetime,
    *,
    page_size: int = 500,
    categories=AI_CATEGORIES,
) -> AsyncIterator[list[PaperCandidate]]:
    query = build_query(categories, start, end)
    offset, total = 0, None
    while total is None or offset < total:
        params = {"search_query": query, "start": offset, "max_results": page_size,
                  "sortBy": "submittedDate", "sortOrder": "descending"}
        try:
            text = await http.get_text(API, params=params)
            total, papers = parse_feed(text)
        except (FetchError, ET.ParseError) as exc:
            log.warning("arxiv.page_failed", offset=offset, error=str(exc))
            return
        log.info("arxiv.page", window=f"{start:%Y-%m-%d}..{end:%Y-%m-%d}", offset=offset,
                 total=total, got=len(papers), with_code=sum(1 for p in papers if p.github_url))
        if not papers:  # arXiv occasionally returns an empty page mid-stream
            return
        yield papers
        offset += len(papers)


async def iter_recent(
    http: HttpFetcher, *, days: int = 30, window_days: int = 1, **kw
) -> AsyncIterator[list[PaperCandidate]]:
    end = datetime.now(timezone.utc)
    for _ in range(0, days, window_days):
        start = end - timedelta(days=window_days)
        async for page in iter_window(http, start, end, **kw):
            yield page
        end = start
