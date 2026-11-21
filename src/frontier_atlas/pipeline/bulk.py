"""Generic bulk-crawl engine for directory sources (Phase I).

A source is two things:

    discover()  -> a list of work items (URLs / ids), cheap: a sitemap, an index, an API listing
    fetch(item) -> list of (key, record) for that item, or [] when the item yields nothing

`crawl()` runs N workers over the frontier with a bounded queue, writes records
in batches, and is resumable: items whose key is already stored are skipped, so a
crashed or interrupted run just continues. Per-host politeness lives in
HttpFetcher, so concurrency here can be high without hammering any one site.

Scaling to 500k is the same code with the frontier in a shared queue (Redis /
SQS) and several worker processes: the store is keyed upsert, so two workers
racing on one item produce one record.
"""
from __future__ import annotations

import asyncio
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Generic, TypeVar

from ..core.http import FetchError
from ..core.logging import get_logger
from ..schemas.base import BaseRecord
from ..storage.db import RecordStore

log = get_logger(__name__)
T = TypeVar("T")


@dataclass
class CrawlStats:
    source: str
    discovered: int = 0
    skipped_existing: int = 0
    fetched: int = 0
    stored: int = 0
    empty: int = 0
    failed: int = 0
    reasons: Counter = field(default_factory=Counter)
    seconds: float = 0.0

    def as_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "reasons"}
        d["reasons"] = dict(self.reasons.most_common(10))
        return d


@dataclass
class BulkSource(Generic[T]):
    name: str
    discover: Callable[[], Awaitable[list[T]]]
    fetch: Callable[[T], Awaitable[list[tuple[str, BaseRecord]]]]
    key_of: Callable[[T], str]          # key the item will be stored under (for resume)
    concurrency: int = 16


async def crawl(
    src: BulkSource,
    store: RecordStore,
    *,
    limit: int | None = None,
    refresh: bool = False,
    batch_size: int = 100,
    existing: Iterable[str] | None = None,
) -> CrawlStats:
    t0 = time.monotonic()
    st = CrawlStats(src.name)
    items = await src.discover()
    st.discovered = len(items)
    have = set(existing) if existing is not None else set()
    if not refresh:
        todo = [it for it in items if src.key_of(it) not in have]
        st.skipped_existing = len(items) - len(todo)
    else:
        todo = list(items)
    if limit is not None:
        todo = todo[: max(0, limit)]
    log.info("bulk.start", source=src.name, discovered=st.discovered, todo=len(todo), skipped=st.skipped_existing)

    q: asyncio.Queue = asyncio.Queue(maxsize=src.concurrency * 4)
    buf: list[tuple[str, BaseRecord]] = []
    lock = asyncio.Lock()

    async def flush(force: bool = False) -> None:
        async with lock:
            if buf and (force or len(buf) >= batch_size):
                rows = buf[:]
                buf.clear()
                st.stored += await store.upsert(rows)
                log.info("bulk.progress", source=src.name, stored=st.stored, done=st.fetched + st.failed, todo=len(todo))

    async def worker() -> None:
        while True:
            it = await q.get()
            if it is None:
                q.task_done()
                return
            try:
                recs = await src.fetch(it)
                st.fetched += 1
                if recs:
                    buf.extend(recs)
                    await flush()
                else:
                    st.empty += 1
            except FetchError as exc:
                st.failed += 1
                st.reasons[f"http_{exc.status}"] += 1
            except Exception as exc:  # one bad page must never kill the crawl
                st.failed += 1
                st.reasons[type(exc).__name__] += 1
                log.warning("bulk.item_failed", source=src.name, item=str(it)[:120], error=f"{type(exc).__name__}: {exc}"[:300])
            finally:
                q.task_done()

    workers = [asyncio.create_task(worker()) for _ in range(src.concurrency)]
    for it in todo:
        await q.put(it)
    for _ in workers:
        await q.put(None)
    await asyncio.gather(*workers)
    await flush(force=True)
    st.seconds = round(time.monotonic() - t0, 1)
    log.info("bulk.done", **{k: v for k, v in st.as_dict().items() if k != "reasons"})
    return st
