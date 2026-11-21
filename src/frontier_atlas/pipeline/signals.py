"""Phase II runner: 5 news sources + 5 job boards, all sources in parallel.

Meant to run on a schedule (every 1-2 h). Each run only ingests items proven
to be < 24 h old, never re-processes anything, and leaves the observations the
next run's "new since last run" heuristic needs.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..core.config import Settings, get_settings
from ..core.http import HttpFetcher
from ..core.protected import ProtectedFetcher
from ..core.logging import get_logger
from ..crawlers.jobs.boards import ALL_BOARDS
from ..crawlers.jobs.crawler import JobCrawler
from ..crawlers.news.crawler import NewsCrawler
from ..crawlers.news.sources import NEWS_SOURCES
from ..export.csv_export import write_csv
from ..extraction import LLMOrchestrator, build_chain
from ..freshness import FreshnessGate, SeenStore
from ..freshness.seen_store import url_key
from ..storage.db import RecordStore

log = get_logger(__name__)
UTC = timezone.utc

NEWS_COLUMNS = [
    "schemaVersion", "recordType", "content.title", "content.published_date", "content.date_source",
    "content.date_confidence", "content.author", "content.companies", "source.name", "source.url", "content.summary",
    "content.text_source", "content.word_count", "content.full_text", "collectedAt",
]
JOB_COLUMNS = [
    "schemaVersion", "recordType", "content.title", "content.company", "content.rawCompany", "content.date", "content.date_source",
    "content.is_remote", "content.role_family", "content.location", "content.ai_keywords",
    "content.extraction", "source.name", "source.url", "collectedAt",
]


def _within(field: str, cutoff: datetime):
    def pred(body: dict) -> bool:
        v = body["content"].get(field)
        return bool(v) and datetime.fromisoformat(v.replace("Z", "+00:00")) >= cutoff
    return pred


async def run(
    *,
    news: bool = True,
    jobs: bool = True,
    only: set[str] | None = None,
    settings: Settings | None = None,
) -> dict:
    s = settings or get_settings()
    now = datetime.now(UTC)
    window = timedelta(hours=s.freshness_window_hours)
    seen = SeenStore(s.seen_db_path)
    gate = FreshnessGate(seen, window=window, node_id=s.node_id)
    store = RecordStore(s.db_path)

    async with HttpFetcher(s) as http:
        llm = None
        if s.gemini_api_key or s.groq_api_key or s.deepseek_api_key:
            llm = LLMOrchestrator(build_chain(s, http.client), s)
        tasks = []
        protected = ProtectedFetcher.from_settings(http)
        if news:
            nc = NewsCrawler(http, gate, protected)
            tasks += [nc.crawl_source(src, now) for src in NEWS_SOURCES if not only or src.name in only]
        if jobs:
            jc = JobCrawler(http, gate, llm)
            tasks += [jc.crawl_board(b(), now) for b in ALL_BOARDS if not only or b.name in only]
        try:
            results = await asyncio.gather(*tasks)
        finally:
            await protected.close()

    all_stats = []
    for records, stats in results:
        all_stats.append(stats.as_dict())
        prefix = "news" if records and records[0].recordType == "NEWS" else "job"
        await store.upsert((f"{prefix}:{url_key(str(r.source.url))}", r) for r in records)

    from .resolve import run_resolution

    resolution = await run_resolution(store, s.output_dir)
    csvs = export_signal_csvs(store, s)
    store.close()
    seen.close()
    return {
        "run_at": now.isoformat(),
        "window_hours": s.freshness_window_hours,
        "sources": all_stats,
        "stored_this_run": sum(st["stored"] for st in all_stats),
        **csvs,
        "entity_resolution": resolution,
        "llm_enabled": llm is not None,
        "protected_fetch": protected.summary(),
    }


def export_signal_csvs(store: RecordStore, s: Settings) -> dict:
    out = Path(s.output_dir)
    cutoff = datetime.now(UTC) - timedelta(hours=s.freshness_window_hours)
    n_news = write_csv(store.iter_bodies("NEWS"), out / "news.csv", columns=NEWS_COLUMNS,
                       where=_within("published_date", cutoff))
    n_jobs = write_csv(store.iter_bodies("JOB"), out / "jobs.csv", columns=JOB_COLUMNS,
                       where=_within("date", cutoff))
    return {"csv_news_rows_24h": n_news, "csv_jobs_rows_24h": n_jobs}
