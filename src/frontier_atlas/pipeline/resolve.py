"""Entity-resolution pass over everything stored, plus the Entity Mapping Log.

All company-name fields across record types are resolved in ONE batch, so the
same company seen in a job post, a product page and a startup directory gets
one canonical identity. The pass is idempotent: it always resolves the *raw*
field (rawCompany / rawStartupName / rawName), never a previous output.

Fields:
    JOB      content.rawCompany      -> content.company
    PRODUCT  content.rawStartupName  -> content.startupName
    STARTUP  content.rawName         -> content.entityName   (+ website domain as evidence)
    NEWS     title + full_text       -> content.companies    (gazetteer mentions)
"""
from __future__ import annotations

import csv
from collections import Counter, defaultdict
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlparse

from ..core.config import Settings, get_settings
from ..core.logging import get_logger
from ..crawlers.bulk.makers import is_platform
from ..resolution.mentions import MentionMatcher
from ..resolution.normalizer import domain_of
from ..resolution.resolver import EntityResolver, Resolution, default_resolver
from ..schemas import JobRecord, NewsRecord, ProductRecord, StartupRecord
from ..storage.db import RecordStore

log = get_logger(__name__)

# record type -> (model, raw field, output field)
NAME_FIELDS = {
    "JOB": (JobRecord, "rawCompany", "company"),
    "PRODUCT": (ProductRecord, "rawStartupName", "startupName"),
    "STARTUP": (StartupRecord, "rawName", "entityName"),
}
LOG_COLUMNS = ["raw_name", "canonical_name", "canonical_id", "method", "score", "is_seed_entity",
               "record_type", "field", "occurrences", "example_source_url"]


def _raw_and_domain(rtype: str, content: dict) -> tuple[str | None, str | None]:
    _, raw_f, out_f = NAME_FIELDS[rtype]
    raw = content.get(raw_f) or content.get(out_f)
    dom = None
    if rtype == "STARTUP":
        dom = domain_of((content.get("data") or {}).get("website"))
    elif rtype == "PRODUCT" and (content.get("startupNameSource") or "").startswith("website:"):
        # the maker was read off the product's own homepage, so that homepage's domain
        # is evidence for the same company (never for a page on a shared platform host)
        web = content.get("website") or ""
        if urlparse(web).path in ("", "/") and not is_platform(web):
            dom = domain_of(web)
    return raw, dom


async def run_resolution(
    store: RecordStore,
    out_dir: str | Path,
    resolver: EntityResolver | None = None,
) -> dict:
    resolver = resolver or default_resolver()
    matcher = MentionMatcher(resolver)

    # 1) collect every raw name across record types
    items: dict[str, list[tuple[str, dict]]] = {t: list(store.iter_items(t)) for t in (*NAME_FIELDS, "NEWS")}
    batch: list[tuple[str, str | None]] = []
    for rtype in NAME_FIELDS:
        for _, body in items[rtype]:
            raw, dom = _raw_and_domain(rtype, body["content"])
            if raw:
                batch.append((raw, dom))
    resolved = resolver.resolve_batch(batch)

    # 2) per-record resolution. A STARTUP's website is its identity: a seed matched by
    #    spelling only but living on other domains is a different company, and two
    #    unknown startups with the same name but different websites stay two entities.
    per_record: dict[tuple[str, str], tuple[str, str, Resolution]] = {}
    site_ids: defaultdict[str, set[str]] = defaultdict(set)
    for rtype in NAME_FIELDS:
        for key, body in items[rtype]:
            raw, dom = _raw_and_domain(rtype, body["content"])
            if not raw or raw not in resolved:
                continue
            res = resolved[raw]
            if rtype == "STARTUP":
                res = resolver.split_by_site(res, raw, dom)
                if res.is_new and dom:
                    site_ids[res.canonical_id].add(dom)
            per_record[(rtype, key)] = (raw, dom or "", res)
    for (rtype, key), (raw, dom, res) in list(per_record.items()):
        if rtype == "STARTUP" and res.is_new and len(site_ids[res.canonical_id]) > 1 and "@" not in res.canonical_id:
            per_record[(rtype, key)] = (raw, dom, replace(res, canonical_id=f"{res.canonical_id}@{dom}"))

    # 3) apply + build the mapping log
    occ: Counter[tuple[str, str, str, str]] = Counter()
    example: dict[tuple[str, str, str, str], str] = {}
    res_of: dict[tuple[str, str, str, str], Resolution] = {}
    updates = []
    for rtype, (model, raw_f, out_f) in NAME_FIELDS.items():
        for key, body in items[rtype]:
            if (rtype, key) not in per_record:
                continue
            raw, _, res = per_record[(rtype, key)]
            c = body["content"]
            c[raw_f] = raw
            c[out_f] = res.canonical
            updates.append((key, model.model_validate(body)))
            k = (raw, rtype, out_f, res.canonical_id)
            occ[k] += 1
            res_of[k] = res
            example.setdefault(k, body["source"]["url"])

    mention_rows: dict[tuple[str, str], list] = {}
    for key, body in items["NEWS"]:
        c = body["content"]
        mentions = matcher.find(c.get("title"), c.get("full_text"))
        c["companies"] = [m.canonical for m in mentions]
        updates.append((key, NewsRecord.model_validate(body)))
        for m in mentions:
            row = mention_rows.setdefault((m.surface, m.canonical_id), [m, 0, body["source"]["url"]])
            row[1] += 1

    await store.upsert(updates)

    rows = []
    for k, n in occ.items():
        raw, rtype, field, _ = k
        r = res_of[k]
        rows.append({
            "raw_name": raw, "canonical_name": r.canonical, "canonical_id": r.canonical_id,
            "method": r.method, "score": r.score, "is_seed_entity": not r.is_new,
            "record_type": rtype, "field": f"content.{field}", "occurrences": n,
            "example_source_url": example[k],
        })
    for (surface, _), (m, n, url) in mention_rows.items():
        rows.append({
            "raw_name": surface, "canonical_name": m.canonical, "canonical_id": m.canonical_id,
            "method": "mention", "score": 100, "is_seed_entity": True, "record_type": "NEWS",
            "field": "content.companies", "occurrences": n, "example_source_url": url,
        })
    # interesting rows first: seed entities whose raw form differs, then everything else
    rows.sort(key=lambda r: (not r["is_seed_entity"], r["raw_name"] == r["canonical_name"],
                             r["canonical_name"].lower(), r["raw_name"].lower()))
    path = Path(out_dir) / "entity_mapping_log.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=LOG_COLUMNS)
        w.writeheader()
        w.writerows(rows)

    methods = Counter(r["method"] for r in rows)
    summary = {
        "names_resolved": len(resolved),
        "canonical_entities": len({r.canonical_id for _, _, r in per_record.values()}),
        "matched_seed": len({raw for raw, _, r in per_record.values() if not r.is_new}),
        "news_with_mentions": sum(1 for _, b in items["NEWS"] if b["content"]["companies"]),
        "methods": dict(methods),
        "mapping_log": str(path),
        "mapping_log_rows": len(rows),
    }
    log.info("resolution.done", **{k: v for k, v in summary.items() if k != "methods"})
    return summary


async def run(settings: Settings | None = None) -> dict:
    """CLI entry: resolve everything stored, then refresh the signal CSVs."""
    from .signals import export_signal_csvs

    s = settings or get_settings()
    store = RecordStore(s.db_path)
    summary = await run_resolution(store, s.output_dir)
    summary.update(export_signal_csvs(store, s))
    store.close()
    return summary
