"""Research-paper pipeline: harvest -> merge -> link code -> live stars -> store -> export.

    HF Daily Papers  ─┐
    arXiv API        ─┼─► merge by arXiv id ─► (optional) HF per-paper code lookup
    paperswithcode.co ┘                         │
                                                ▼
                                   GitHub metrics (GraphQL ×100 / REST)
                                                ▼
                          PaperRecord ─► SQLite upsert ─► CSV (Sheets tab)

No LLM is involved: every field comes from a structured source (API / JSON-LD),
so nothing can be hallucinated, and every record keeps its source URL.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from ..core.config import Settings, get_settings
from ..core.http import HttpFetcher
from ..core.logging import get_logger
from ..crawlers.bulk import papers_arxiv, papers_hf, papers_pwc
from ..crawlers.bulk.github_links import repo_url
from ..crawlers.bulk.github_metrics import GitHubMetrics
from ..crawlers.bulk.paper_types import PaperCandidate
from ..export.csv_export import PAPER_COLUMNS, write_csv
from ..schemas import PaperContent, PaperRecord, Source
from ..storage.db import RecordStore

log = get_logger(__name__)

# Which source wins for the code link: author-linked (HF) > in-abstract (arXiv) > page scrape (PwC)
REPO_PRIORITY = {"huggingface": 0, "arxiv": 1, "paperswithcode": 2}
SOURCE_LABEL = {"huggingface": "Hugging Face Papers", "arxiv": "arXiv", "paperswithcode": "Papers with Code"}


@dataclass
class PaperRunConfig:
    target_with_code: int = 1000
    hf_start: date | None = None
    hf_max_days: int = 120
    arxiv_days: int = 0          # extra breadth from arXiv (0 = skip)
    hf_link_lookups: int = 0     # per-paper HF lookups to find code for arXiv-only papers
    pwc_pages: int = 0           # pages from paperswithcode.co sitemaps
    pwc_urls: tuple[str, ...] = ()


class PaperMerger:
    def __init__(self) -> None:
        self.by_key: dict[str, PaperCandidate] = {}
        self.sources: dict[str, set[str]] = {}

    def add(self, c: PaperCandidate) -> None:
        k = c.key
        self.sources.setdefault(k, set()).add(c.source_name)
        cur = self.by_key.get(k)
        if cur is None:
            self.by_key[k] = c
            return
        # bibliographic fields: arXiv is authoritative
        if c.source_name == "arxiv":
            cur.title, cur.authors = c.title, c.authors or cur.authors
            cur.published = c.published or cur.published
            cur.abstract = c.abstract or cur.abstract
        else:
            cur.authors = cur.authors or c.authors
            cur.published = cur.published or c.published
        # code link: higher-priority source wins
        if c.github_url and (
            not cur.github_url or REPO_PRIORITY[c.source_name] < REPO_PRIORITY[cur.source_name]
        ):
            cur.github_url, cur.github_stars = c.github_url, c.github_stars
            cur.source_name, cur.source_url = c.source_name, c.source_url

    @property
    def with_code(self) -> int:
        return sum(1 for c in self.by_key.values() if c.github_url)


def to_record(c: PaperCandidate, sources: set[str], metrics) -> PaperRecord:
    slug = c.github_url.removeprefix("https://github.com/") if c.github_url else None
    m = metrics.get(slug) if slug else None
    stars, stars_source, checked, exists, gh_url = c.github_stars, None, None, None, c.github_url
    if m is not None:
        exists, checked = m.exists, m.checked_at
        if m.exists:
            stars, stars_source, gh_url = m.stars, "github_api", repo_url(m.slug)
        else:
            stars = None
    elif stars is not None:
        stars_source = "huggingface"
    published = c.published
    if published is not None and published.tzinfo is None:  # date-only sources -> explicit UTC
        published = published.replace(tzinfo=timezone.utc)
    return PaperRecord(
        source=Source(name=SOURCE_LABEL[c.source_name], url=c.source_url),
        content=PaperContent(
            title=c.title,
            authors=c.authors,
            paper_url=c.paper_url or c.source_url,
            github_url=gh_url,
            github_stars=stars,
            github_stars_source=stars_source,
            github_stars_checked_at=checked,
            github_repo_exists=exists,
            published_date=published,
            arxiv_id=c.arxiv_id,
            abstract=c.abstract,
            sources=sorted(SOURCE_LABEL[s] for s in sources),
        ),
    )


async def run(cfg: PaperRunConfig, settings: Settings | None = None) -> dict:
    s = settings or get_settings()
    store = RecordStore(s.db_path)
    merger = PaperMerger()
    t0 = datetime.now(timezone.utc)

    async with HttpFetcher(s) as http:
        # 1) HF Daily Papers until we have enough papers with code
        start = cfg.hf_start or datetime.now(timezone.utc).date()
        async for day_papers in papers_hf.iter_days(http, start, max_days=cfg.hf_max_days):
            for p in day_papers:
                merger.add(p)
            if merger.with_code >= cfg.target_with_code:
                break
        log.info("papers.hf_done", papers=len(merger.by_key), with_code=merger.with_code)

        # 2) arXiv breadth
        if cfg.arxiv_days:
            async for page in papers_arxiv.iter_recent(http, days=cfg.arxiv_days):
                for p in page:
                    merger.add(p)
            log.info("papers.arxiv_done", papers=len(merger.by_key), with_code=merger.with_code)

        # 3) find code for arXiv-only papers via HF per-paper API
        if cfg.hf_link_lookups:
            todo = [c for c in merger.by_key.values() if not c.github_url and c.arxiv_id][: cfg.hf_link_lookups]
            found = await asyncio.gather(*(papers_hf.lookup_paper(http, c.arxiv_id) for c in todo))
            for f in found:
                if f and f.github_url:
                    merger.add(f)
            log.info("papers.hf_lookup_done", looked_up=len(todo), with_code=merger.with_code)

        # 4) paperswithcode.co pages
        urls = list(cfg.pwc_urls)
        if cfg.pwc_pages:
            async for u in papers_pwc.iter_paper_urls(http):
                urls.append(u)
                if len(urls) >= cfg.pwc_pages + len(cfg.pwc_urls):
                    break
        if urls:
            pages = await asyncio.gather(*(papers_pwc.fetch_paper(http, u) for u in urls))
            for p in pages:
                if p:
                    merger.add(p)
            log.info("papers.pwc_done", pages=len(urls), with_code=merger.with_code)

        # 5) live GitHub stars
        slugs = [c.github_url.removeprefix("https://github.com/")
                 for c in merger.by_key.values() if c.github_url]
        metrics = await GitHubMetrics(http, s.github_token).fetch(slugs)
        log.info("papers.github_done", repos=len(set(slugs)), refreshed=len(metrics))
        http_stats = dict(http.stats)

    # 6) store + export
    records = [(k, to_record(c, merger.sources[k], metrics)) for k, c in merger.by_key.items()]
    await store.upsert(records)
    out_dir = Path(s.output_dir)
    n_code = write_csv(store.iter_bodies("RESEARCH_PAPER"), out_dir / "research_papers.csv",
                       columns=PAPER_COLUMNS, where=lambda b: bool(b["content"].get("github_url")))
    n_all = write_csv(store.iter_bodies("RESEARCH_PAPER"), out_dir / "research_papers_all.csv",
                      columns=PAPER_COLUMNS)
    summary = {
        "papers_this_run": len(records),
        "with_code_this_run": merger.with_code,
        "stored_total": n_all,
        "stored_with_code": n_code,
        "stars_live_from_github": sum(1 for m in metrics.values() if m.exists),
        "seconds": round((datetime.now(timezone.utc) - t0).total_seconds(), 1),
        "http": http_stats,
        "csv": str(out_dir / "research_papers.csv"),
    }
    store.close()
    return summary
