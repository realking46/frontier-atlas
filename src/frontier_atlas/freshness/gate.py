"""FreshnessGate – the single entry point news/job crawlers use.

    run = gate.begin("techcrunch-ai")
    decisions = gate.screen_listing(run, [ListingItem(url, date_text), ...])
    for d in decisions:
        if d.verdict is Verdict.FRESH and d.needs_page is False: fetch + extract
        if d.needs_page: html = fetch(d.url); gate.screen_page(d, html, headers)
        ...
        if d.verdict is Verdict.FRESH and gate.finalize(d, full_text): store record
    gate.end(run)

Guarantees:
  * an item is ingested only with verdict FRESH (proven inside the window)
  * an item is processed at most once across all nodes (claim/lease + done)
  * syndicated near-copies are dropped (SimHash)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ..core.logging import get_logger
from .date_parser import DateResult, extract_published, parse_date
from .heuristics import Verdict, bracket_by_listing, classify, new_since_last_run
from .seen_store import RunInfo, SeenStore, canonical_url, simhash, url_key

log = get_logger(__name__)
UTC = timezone.utc


@dataclass(slots=True)
class ListingItem:
    url: str
    date_text: str | None = None         # whatever date text the listing shows, if any
    date: DateResult | None = None       # or an already-parsed date (e.g. RSS pubDate)


@dataclass
class Decision:
    url: str
    key: str
    source: str
    verdict: Verdict
    reason: str
    date: DateResult | None = None
    heuristic: Verdict = Verdict.UNCERTAIN   # verdict from undated-content heuristics
    needs_page: bool = False                 # date unknown at listing level -> look on the page
    claimed: bool = False
    extra: dict = field(default_factory=dict)

    @property
    def published_at(self) -> datetime | None:
        return self.date.dt if self.date else None


class FreshnessGate:
    def __init__(
        self,
        store: SeenStore,
        *,
        window: timedelta = timedelta(hours=24),
        node_id: str = "node-0",
        min_confidence: float = 0.5,
        near_dup_distance: int = 3,
        default_tz=UTC,
    ):
        self.store = store
        self.window = window
        self.node_id = node_id
        self.min_conf = min_confidence
        self.near_dup_distance = near_dup_distance
        self.default_tz = default_tz

    # ------------------------------------------------------------ run lifecycle
    def begin(self, source: str, now: datetime | None = None) -> RunInfo:
        return self.store.start_run(source, now)

    def end(self, run: RunInfo, ok: bool = True) -> None:
        self.store.finish_run(run, ok)

    # ------------------------------------------------------------ stage 1: listing
    def screen_listing(self, run: RunInfo, items: list[ListingItem], now: datetime | None = None) -> list[Decision]:
        now = now or datetime.now(UTC)
        source = run.source
        keys = [url_key(it.url) for it in items]
        dates: list[DateResult | None] = []
        for it in items:
            d = it.date
            if d is None and it.date_text:
                p = parse_date(it.date_text, now, default_tz=self.default_tz)
                if p:
                    d = DateResult(p.dt, "listing", 0.75, it.date_text[:80], p.date_only, p.relative, p.granularity_s)
            dates.append(d)

        # heuristics need the observation state *before* this run is recorded
        prev = self.store.last_successful_run(source, before_run=run.run_id)
        unseen = self.store.observe(source, keys, run)
        nslr = new_since_last_run(keys, unseen, prev.started_at if prev else None, now, self.window)
        bracket = bracket_by_listing(dates, now, self.window)

        decisions: list[Decision] = []
        for it, key, d, br in zip(items, keys, dates, bracket):
            status = self.store.status(key)
            if status in ("done", "rejected"):
                decisions.append(Decision(it.url, key, source, Verdict.DUPLICATE, f"already {status}", d))
                continue
            heuristic = br or nslr.get(key, Verdict.UNCERTAIN)
            verdict = classify(d, now, self.window, min_confidence=self.min_conf) if d else Verdict.UNCERTAIN
            dec = Decision(it.url, key, source, verdict, "", d, heuristic)
            if verdict is Verdict.STALE:
                dec.reason = f"listing date {d.iso} older than {self.window}"
            elif verdict is Verdict.FRESH:
                dec.reason = f"listing date {d.iso} ({d.source})"
            else:
                dec.needs_page = True   # look for a precise date on the article page
                dec.reason = "no conclusive listing date"
            if verdict is not Verdict.STALE:
                dec.claimed = self.store.claim(key, source=source, url=canonical_url(it.url), node=self.node_id)
                if not dec.claimed:
                    dec.verdict, dec.reason, dec.needs_page = Verdict.DUPLICATE, "claimed by another node", False
            decisions.append(dec)

        counts = {v.value: sum(1 for x in decisions if x.verdict is v) for v in Verdict}
        log.info("freshness.listing", source=source, items=len(items), unseen=len(unseen),
                 prev_run=prev.started_at.isoformat() if prev else None, **counts)
        return decisions

    # ------------------------------------------------------------ stage 2: article page
    def screen_page(self, dec: Decision, html: str, headers: dict | None = None,
                    now: datetime | None = None) -> Decision:
        now = now or datetime.now(UTC)
        page_date = extract_published(html, dec.url, headers=headers, now=now, default_tz=self.default_tz)
        # Prefer the page date when it is at least as reliable as the listing date.
        if page_date and (dec.date is None or page_date.confidence >= dec.date.confidence
                          or dec.verdict is Verdict.UNCERTAIN):
            dec.date = page_date
        verdict = classify(dec.date, now, self.window, min_confidence=self.min_conf)
        if verdict is Verdict.UNCERTAIN and dec.heuristic is not Verdict.UNCERTAIN:
            verdict = dec.heuristic
            dec.reason = f"undated/imprecise; heuristic says {verdict.value}"
            if verdict is Verdict.FRESH and dec.date is None:
                # best available timestamp: first time we ever saw the URL
                fs = self.store.first_seen(dec.source, dec.key)
                if fs:
                    dec.date = DateResult(fs, "heuristic:first_seen", 0.5, "first observed")
        elif dec.date is not None:
            dec.reason = f"{dec.date.source} = {dec.date.iso}"
        else:
            dec.reason = "no date found and no heuristic proof"
        dec.verdict, dec.needs_page = verdict, False
        return dec

    # ------------------------------------------------------------ stage 3: commit
    def finalize(self, dec: Decision, text: str | None = None, *, label: str | None = None) -> bool:
        """Call with the extracted full text right before storing. Returns False if
        the item must not be stored (not fresh, or near-duplicate of stored content).
        `label` (e.g. job title) must also match for an item to count as a duplicate."""
        if not dec.claimed:
            return False
        if dec.verdict is not Verdict.FRESH:
            self.store.mark_rejected(dec.key, f"{dec.verdict.value}: {dec.reason}")
            return False
        h = simhash(text) if text and len(text.split()) >= 30 else None
        if h is not None and (dup := self.store.find_near_duplicate(
                h, max_distance=self.near_dup_distance, label=label)):
            dec.verdict, dec.reason = Verdict.DUPLICATE, f"near-duplicate of {dup}"
            self.store.mark_rejected(dec.key, dec.reason)
            return False
        self.store.mark_done(dec.key, simhash_value=h, published_at=dec.published_at, label=label)
        return True

    def reject(self, dec: Decision, reason: str) -> None:
        """Final 'no' for a reason outside freshness (e.g. not an AI job)."""
        if dec.claimed:
            self.store.mark_rejected(dec.key, reason)
        dec.reason = reason

    def release(self, dec: Decision) -> None:
        """Transient failure (fetch error): give the item back for a later retry."""
        if dec.claimed:
            self.store.release(dec.key)
            dec.claimed = False


class InOrder:
    """Lets concurrent workers run a critical step in a fixed order.

    Pages are fetched concurrently, but the near-duplicate claim (first copy wins)
    must not depend on which download happened to finish first, or reruns would
    keep different copies. Worker i calls `await turn(i)` before claiming and
    `await done(i)` when finished (always, in a finally).
    """

    def __init__(self, n: int):
        import asyncio

        self._ev = [asyncio.Event() for _ in range(n + 1)]
        self._ev[0].set()

    async def turn(self, i: int) -> None:
        await self._ev[i].wait()

    async def done(self, i: int) -> None:
        """Mark worker i finished - only after every earlier worker has finished, so an item
        that exits early can never let a later item claim ahead of an earlier, slower one."""
        await self._ev[i].wait()
        self._ev[i + 1].set()
