"""Hugging Face Daily Papers – successor of paperswithcode.com's paper<->code links.

GET https://huggingface.co/api/daily_papers?date=YYYY-MM-DD
returns every curated paper of that day with `githubRepo` and `githubStars`
when the authors linked their code. Rate limit: 500 requests / 5 min,
advertised in the `RateLimit` header (handled by HttpFetcher).
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import date, datetime, timedelta

from ...core.http import FetchError, HttpFetcher
from ...core.logging import get_logger
from .github_links import find_repos, repo_url
from .paper_types import PaperCandidate, arxiv_id_from

log = get_logger(__name__)
API = "https://huggingface.co/api/daily_papers"
PAPER_API = "https://huggingface.co/api/papers/{id}"


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _to_candidate(paper: dict, source_url: str) -> PaperCandidate | None:
    pid = paper.get("id")
    title = (paper.get("title") or "").strip()
    if not pid or not title:
        return None
    aid = arxiv_id_from(pid)
    repos = find_repos(paper.get("githubRepo") or "")
    if not repos:  # some authors put the repo in projectPage instead
        repos = find_repos(paper.get("projectPage") or "")
    authors = [a.get("name", "").strip() for a in paper.get("authors") or [] if not a.get("hidden")]
    return PaperCandidate(
        source_name="huggingface",
        source_url=source_url,
        title=" ".join(title.split()),
        authors=[a for a in authors if a],
        arxiv_id=aid,
        abstract=(paper.get("summary") or "").strip() or None,
        published=_parse_dt(paper.get("publishedAt")),
        paper_url=f"https://arxiv.org/abs/{aid}" if aid else f"https://huggingface.co/papers/{pid}",
        github_url=repo_url(repos[0]) if repos else None,
        github_stars=paper.get("githubStars") if repos else None,
        repo_candidates=repos,
    )


async def fetch_day(http: HttpFetcher, day: date) -> list[PaperCandidate]:
    url = f"{API}?date={day.isoformat()}&limit=100"
    try:
        data = await http.get_json(url)
    except FetchError as exc:
        log.warning("hf.day_failed", day=str(day), error=str(exc))
        return []
    out = []
    for item in data or []:
        paper = item.get("paper") or item
        if c := _to_candidate(paper, f"https://huggingface.co/papers/{paper.get('id')}"):
            out.append(c)
    return out


async def iter_days(
    http: HttpFetcher,
    start: date,
    *,
    max_days: int = 365,
    concurrency: int = 6,
) -> AsyncIterator[list[PaperCandidate]]:
    """Walk backwards day by day, `concurrency` days in flight at a time.
    The caller stops iterating once it has enough records."""
    day = start
    end = start - timedelta(days=max_days)
    while day > end:
        batch = [day - timedelta(days=i) for i in range(concurrency) if day - timedelta(days=i) > end]
        results = await asyncio.gather(*(fetch_day(http, d) for d in batch))
        for d, papers in zip(batch, results):
            log.info("hf.day", day=str(d), papers=len(papers), with_code=sum(1 for p in papers if p.github_url))
            yield papers
        day -= timedelta(days=len(batch))


async def lookup_paper(http: HttpFetcher, arxiv_id: str) -> PaperCandidate | None:
    """Per-paper lookup, used to find code for arXiv papers that HF knows about."""
    url = PAPER_API.format(id=arxiv_id)
    try:
        data = await http.get_json(url, max_retries=2)
    except FetchError as exc:
        if exc.status != 404:
            log.debug("hf.lookup_failed", id=arxiv_id, error=str(exc))
        return None
    return _to_candidate(data, f"https://huggingface.co/papers/{arxiv_id}")
