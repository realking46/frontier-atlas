"""Phase I runner for startups and products (bulk directory crawl).

    startups:  YC company pages              -> STARTUP records
    products:  Futurepedia tools + OpenRouter -> PRODUCT records (maker looked up per product)

Both are resumable (already-stored keys are skipped unless --refresh), then
entity resolution runs over everything and the CSVs are re-exported.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from ..core.config import Settings, get_settings
from ..core.http import HttpFetcher
from ..core.protected import ProtectedFetcher
from ..core.logging import get_logger
from ..crawlers.bulk import products as P
from ..crawlers.bulk import startups as S
from ..crawlers.bulk.makers import registrable_domain
from ..export.csv_export import write_csv
from ..storage.db import RecordStore
from .bulk import BulkSource, crawl

log = get_logger(__name__)

STARTUP_COLUMNS = [
    "schemaVersion", "recordType", "content.entityName", "content.rawName", "content.data.employeeCount",
    "content.data.description", "content.data.website", "content.data.foundedYear", "content.data.location",
    "content.data.industries", "content.data.status", "content.data.batch", "content.data.founders",
    "content.data.linkedin", "source.name", "source.url", "collectedAt",
]
PRODUCT_COLUMNS = [
    "schemaVersion", "recordType", "content.productName", "content.startupName", "content.rawStartupName",
    "content.startupNameSource", "content.startupEvidenceUrl", "content.pricingModel", "content.pricingRaw",
    "content.description", "content.website", "content.categories", "source.name", "source.url", "collectedAt",
]

YC_HOST_INTERVAL = 0.2      # ~5 req/s to ycombinator.com
FP_HOST_INTERVAL = 0.25     # ~4 req/s to futurepedia.io


def startup_domains(store: RecordStore) -> dict[str, str]:
    out: dict[str, str] = {}
    for body in store.iter_bodies("STARTUP"):
        dom = registrable_domain((body["content"].get("data") or {}).get("website"))
        if dom:
            out.setdefault(dom, body["content"]["entityName"])
    return out


async def run_startups(*, limit: int | None = None, refresh: bool = False, settings: Settings | None = None,
                       store: RecordStore | None = None) -> dict:
    s = settings or get_settings()
    own = store is None
    store = store or RecordStore(s.db_path)
    async with HttpFetcher(s) as http:
        http.set_host_interval(S.YC_COMPANY_URL, YC_HOST_INTERVAL)
        src = BulkSource(
            name="yc-directory",
            discover=lambda: S.discover_slugs(http),
            fetch=lambda slug: S.fetch_company(http, slug),
            key_of=S.startup_key,
            concurrency=8,
        )
        st = await crawl(src, store, limit=limit, refresh=refresh, existing=store.existing_keys("STARTUP"))
    out = {"startups": st.as_dict(), "http": dict(http.stats)}
    out.update(await _finish(store, s))
    if own:
        store.close()
    return out


async def run_products(*, limit: int | None = None, refresh: bool = False, sources: set[str] | None = None,
                       settings: Settings | None = None, store: RecordStore | None = None) -> dict:
    s = settings or get_settings()
    own = store is None
    store = store or RecordStore(s.db_path)
    out: dict = {}
    async with HttpFetcher(s) as http:
        if not sources or "openrouter" in sources:
            recs = await P.or_fetch_all(http)
            await store.upsert(recs)
            out["openrouter"] = {"stored": len(recs)}
        if not sources or "futurepedia" in sources:
            http.set_host_interval(P.FP_SITEMAP, FP_HOST_INTERVAL)
            makers = P.MakerLookup(http, startup_domains(store))
            src = BulkSource(
                name="futurepedia",
                discover=lambda: P.fp_discover(http),
                fetch=lambda url: P.fp_fetch(http, makers, url),
                key_of=lambda url: P.product_key("futurepedia", P.fp_slug(url)),
                concurrency=24,      # futurepedia itself is rate-limited per host; vendor sites run in parallel
            )
            st = await crawl(src, store, limit=limit, refresh=refresh, existing=store.existing_keys("PRODUCT"))
            out["futurepedia"] = st.as_dict()
        out["http"] = dict(http.stats)
    out.update(await _finish(store, s))
    if own:
        store.close()
    return out


async def retry_makers(*, limit: int | None = None, settings: Settings | None = None,
                       store: RecordStore | None = None) -> dict:
    """Second pass for products whose maker could not be read the first time, now through
    the protected fetcher (robots.txt, JS rendering, block detection + circuit breaker)."""
    from ..schemas import ProductRecord

    s = settings or get_settings()
    own = store is None
    store = store or RecordStore(s.db_path)
    todo = [(k, b) for k, b in store.iter_items("PRODUCT")
            if k.startswith("product:futurepedia:") and not b["content"].get("startupName") and b["content"].get("website")]
    if limit is not None:
        todo = todo[:limit]
    found = []
    async with HttpFetcher(s) as http:
        pf = ProtectedFetcher.from_settings(http)
        makers = P.MakerLookup(http, startup_domains(store), fetcher=pf)
        sem = asyncio.Semaphore(16)

        async def one(key: str, body: dict) -> None:
            async with sem:
                m = await makers.find(body["content"]["website"])
            if m:
                c = body["content"]
                c.update(startupName=m.name, rawStartupName=m.name, startupNameSource=m.method,
                         startupEvidenceUrl=m.evidence_url)
                found.append((key, ProductRecord.model_validate(body)))
        try:
            await asyncio.gather(*(one(k, b) for k, b in todo))
        finally:
            await pf.close()
        await store.upsert(found)
        out = {"retried": len(todo), "makers_found": len(found), "outcomes": dict(makers.outcomes),
               "by_tier": {t: sum(c.get(f"ok:{t}", 0) for c in pf.stats.values()) for t in ("http", "browser")},
               "blocked_hosts": sum(1 for c in pf.stats.values() if any(k.startswith("block:") for k in c))}
    out.update(await _finish(store, s))
    if own:
        store.close()
    return out


async def _finish(store: RecordStore, s: Settings) -> dict:
    from .resolve import run_resolution

    res = await run_resolution(store, s.output_dir)
    return {"entity_resolution": res, **export_catalog_csvs(store, s)}


def export_catalog_csvs(store: RecordStore, s: Settings) -> dict:
    out = Path(s.output_dir)
    return {
        "csv_startups": write_csv(store.iter_bodies("STARTUP"), out / "startups.csv", columns=STARTUP_COLUMNS),
        "csv_products": write_csv(store.iter_bodies("PRODUCT"), out / "products.csv", columns=PRODUCT_COLUMNS),
    }
