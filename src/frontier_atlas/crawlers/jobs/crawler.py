"""Job crawler: board pages -> freshness gate -> (detail page) -> AI filter -> JobRecord.

Paging stops as soon as a whole page is STALE/DUPLICATE (boards list newest
first), so a 24-hour crawl of an 800-jobs/day board costs ~40 requests, and an
incremental hourly run costs 1-2.

Field provenance, in order of preference:
  structured   board API / RSS / JSON-LD JobPosting
  llm:<name>   LLM extraction (only when structured data lacks title/company,
               grounded against the page text – see extraction/grounding.py)
  rules        title regex fallback when no LLM is configured
`role_family` and `is_remote` are deterministic rules over title/location/text.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from ...core.errors import AllProvidersFailedError, NoEntityFoundError
from ...core.http import FetchError, HttpFetcher
from ...core.logging import get_logger
from ...extraction.orchestrator import LLMOrchestrator
from ...freshness import FreshnessGate, ListingItem, Verdict
from ...freshness.gate import Decision, InOrder
from ...schemas import JobContent, JobExtraction, JobRecord, Source
from ..signal_stats import SourceStats
from .boards import JobBoard, JobPosting
from .classify import ai_relevance, infer_remote, role_family

log = get_logger(__name__)
UTC = timezone.utc


class JobCrawler:
    def __init__(self, http: HttpFetcher, gate: FreshnessGate, llm: LLMOrchestrator | None = None):
        self.http = http
        self.gate = gate
        self.llm = llm

    async def crawl_board(self, board: JobBoard, now: datetime | None = None) -> tuple[list[JobRecord], SourceStats]:
        now = now or datetime.now(UTC)
        stats = SourceStats(board.name)
        run = self.gate.begin(board.name, now)
        records: list[JobRecord] = []
        seen_urls: set[str] = set()
        ok = False
        for attr in ("API", "FEED", "LISTING"):       # board-specific politeness for its listing host
            if (url := getattr(board, attr, None)) and board.min_interval_s:
                self.http.set_host_interval(url, board.min_interval_s)
        try:
            async for page in board.pages(self.http, now):
                page = [p for p in page if p.url not in seen_urls]
                seen_urls.update(p.url for p in page)
                if not page:
                    continue
                stats.listed += len(page)
                by_url = {p.url: p for p in page}
                decisions = self.gate.screen_listing(run, [ListingItem(p.url, date=p.date) for p in page], now)
                live = []
                for d in decisions:
                    if d.verdict is Verdict.STALE:
                        stats.stale += 1
                    elif d.verdict is Verdict.DUPLICATE:
                        stats.duplicate += 1
                    else:
                        live.append(d)
                order = InOrder(len(live))

                async def ordered(i: int, d: Decision, by_url=by_url, order=order):
                    try:
                        return await self._process(board, d, by_url[d.url], now, stats, turn=lambda: order.turn(i))
                    finally:
                        await order.done(i)
                results = await asyncio.gather(*(ordered(i, d) for i, d in enumerate(live)))
                records.extend(r for r in results if r is not None)
                if not live:          # newest-first listing: everything further down is older
                    break
            ok = True
        except (FetchError, ValueError, SyntaxError, KeyError) as exc:
            stats.errors.append(f"listing: {type(exc).__name__}: {exc}")
            log.error("jobs.board_failed", board=board.name, error=str(exc))
        finally:
            self.gate.end(run, ok=ok)
        stats.stored = len(records)
        log.info("jobs.board_done", **{k: v for k, v in stats.as_dict().items() if k != "errors"})
        return records, stats

    async def _process(self, board: JobBoard, d: Decision, post: JobPosting, now: datetime,
                       stats: SourceStats, turn=None) -> JobRecord | None:
        html = None
        if board.needs_detail or d.needs_page:
            try:
                resp = await self.http.get(post.url)
                html = resp.text
            except FetchError as exc:
                self.gate.release(d)
                stats.errors.append(f"{post.url}: {exc.status}")
                return None
            if d.needs_page:
                self.gate.screen_page(d, html, dict(resp.headers), now)
            if board.needs_detail:
                await board.enrich(self.http, post, html)

        if d.verdict is not Verdict.FRESH:
            stats.stale += d.verdict is Verdict.STALE
            stats.uncertain += d.verdict is Verdict.UNCERTAIN
            self.gate.finalize(d)
            return None

        if (not post.title or not post.company) and self.llm and post.description:
            await self._llm_fill(post)

        relevant, matched = ai_relevance(post.title or "", post.tags, post.description)
        if not relevant:
            stats.not_ai += 1
            self.gate.reject(d, "not an AI job")
            return None
        if not post.title or not post.company:
            stats.uncertain += 1
            self.gate.reject(d, "title/company not extractable")
            return None

        if turn:
            await turn()                  # near-dup claims happen in listing order
        if not self.gate.finalize(d, post.description, label=post.title):
            stats.near_duplicate += 1     # same job cross-posted on another board
            return None
        stats.fresh += 1
        remote = post.remote if post.remote is not None else infer_remote(post.location, post.title, post.description[:3000])
        return JobRecord(
            source=Source(name=board.label, url=post.url),
            content=JobContent(
                title=post.title,
                company=post.company,
                rawCompany=post.company,
                date=d.date.dt,
                date_source=d.date.source,
                is_remote=remote,
                role_family=role_family(post.title),
                location=post.location,
                ai_keywords=matched[:8],
                extraction=post.extraction,
            ),
        )

    async def _llm_fill(self, post: JobPosting) -> None:
        try:
            res = await self.llm.extract(post.description, JobExtraction, source_url=post.url)
        except (NoEntityFoundError, AllProvidersFailedError) as exc:
            log.info("jobs.llm_no_result", url=post.url, error=type(exc).__name__)
            return
        post.title = post.title or res.data.title
        post.company = post.company or res.data.company
        post.location = post.location or res.data.location
        if post.remote is None and res.data.is_remote is not None:
            post.remote = res.data.is_remote
        post.extraction = f"llm:{res.provider}"
