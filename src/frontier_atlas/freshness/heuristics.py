"""Freshness verdicts – with and without a reliable date.

Verdicts are three-valued on purpose:
    FRESH      provably published inside the window
    STALE      provably published before the window
    UNCERTAIN  cannot be proven either way -> NOT ingested (the brief demands a
               guarantee), but the URL is still recorded so the next run can
               reason about it.

Undated content is resolved with two independent heuristics:

A. **Listing bracketing** – most news/job listings are reverse-chronological.
   An undated item sitting *above* a dated item cannot be older than it. If
   the dated neighbour below is itself inside the window, the undated item is
   provably fresh. We only trust this when the dated items on the page really
   are ordered (>= 80 % of adjacent pairs non-increasing), which filters out
   "popular"/"featured" lists and pinned posts.

B. **New since last run (frontier rule)** – each run records every listing URL
   it saw. On the next run, a URL that was never observed before AND appears
   above the first already-known URL (the "frontier") was published after the
   previous run started. If that previous run is itself inside the window
   (we schedule runs every 1–2 h), the item is provably fresh. Cold start (no
   previous run) or a gap longer than the window -> UNCERTAIN.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from .date_parser import DateResult


class Verdict(str, Enum):
    FRESH = "FRESH"
    STALE = "STALE"
    UNCERTAIN = "UNCERTAIN"
    DUPLICATE = "DUPLICATE"


@dataclass(slots=True)
class Interval:
    earliest: datetime
    latest: datetime


def possible_interval(d: DateResult) -> Interval:
    """When could the item really have been published, given the date's precision?"""
    g = timedelta(seconds=d.granularity_s)
    if d.relative and not d.date_only:
        return Interval(d.dt - g, d.dt)        # "3 hours ago" -> 3h..4h ago
    if d.date_only:
        return Interval(d.dt, d.dt + g)        # "2026-11-19" -> any time that day
    return Interval(d.dt, d.dt)


def classify(d: DateResult | None, now: datetime, window: timedelta, *, min_confidence: float = 0.5) -> Verdict:
    if d is None:
        return Verdict.UNCERTAIN
    cutoff = now - window
    iv = possible_interval(d)
    if iv.latest < cutoff:
        return Verdict.STALE           # even the latest possible time is too old
    if d.confidence < min_confidence:
        return Verdict.UNCERTAIN       # weak evidence can only ever reject
    if iv.earliest >= cutoff:
        return Verdict.FRESH
    return Verdict.UNCERTAIN


def is_reverse_chronological(dates: list[datetime], *, min_ratio: float = 0.8) -> bool:
    if len(dates) < 3:
        return False
    pairs = list(zip(dates, dates[1:]))
    ok = sum(1 for a, b in pairs if a >= b - timedelta(minutes=5))
    return ok / len(pairs) >= min_ratio


def bracket_by_listing(
    dates: list[DateResult | None], now: datetime, window: timedelta
) -> list[Verdict | None]:
    """Heuristic A. Returns a verdict for undated positions (None where not applicable)."""
    dated = [(i, possible_interval(d)) for i, d in enumerate(dates) if d is not None]
    out: list[Verdict | None] = [None] * len(dates)
    if not is_reverse_chronological([iv.earliest for _, iv in dated]):
        return out
    cutoff = now - window
    for i, d in enumerate(dates):
        if d is not None:
            continue
        below = next((iv for j, iv in dated if j > i), None)   # nearest dated item further down
        above = next((iv for j, iv in reversed(dated) if j < i), None)
        if below is not None and below.earliest >= cutoff:
            out[i] = Verdict.FRESH        # newer than something already fresh
        elif above is not None and above.latest < cutoff:
            out[i] = Verdict.STALE        # older than something already stale
    return out


def frontier_new_keys(listing_keys: list[str], unseen: set[str]) -> set[str]:
    """Heuristic B core: unseen keys positioned above the first previously-seen key."""
    frontier = next((i for i, k in enumerate(listing_keys) if k not in unseen), len(listing_keys))
    return {k for k in listing_keys[:frontier] if k in unseen}


def new_since_last_run(
    listing_keys: list[str],
    unseen: set[str],
    last_run_started: datetime | None,
    now: datetime,
    window: timedelta,
) -> dict[str, Verdict]:
    if last_run_started is None or last_run_started < now - window:
        return {k: Verdict.UNCERTAIN for k in listing_keys}   # cold start / too long a gap
    fresh = frontier_new_keys(listing_keys, unseen)
    if len(listing_keys) > 3 and len(fresh) == len(listing_keys):
        # no known item left on the page: reshuffled/rotated listing, not proof of newness
        return {k: Verdict.UNCERTAIN for k in listing_keys}
    return {k: (Verdict.FRESH if k in fresh else Verdict.UNCERTAIN) for k in listing_keys}
