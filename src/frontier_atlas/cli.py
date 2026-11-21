"""Quick manual test of the LLM engine on one page.

    python -m frontier_atlas.cli extract --type startup --url https://example.com/about
    python -m frontier_atlas.cli extract --type job --file saved_page.html
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import httpx

from .core.config import get_settings
from .core.errors import AllProvidersFailedError, NoEntityFoundError
from .core.logging import configure_logging
from .extraction import LLMOrchestrator, build_chain, html_to_text
from .schemas import JobExtraction, NewsExtraction, ProductExtraction, StartupExtraction

SCHEMAS = {
    "startup": StartupExtraction,
    "product": ProductExtraction,
    "job": JobExtraction,
    "news": NewsExtraction,
}
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"


async def _extract(args: argparse.Namespace) -> int:
    s = get_settings()
    if not (s.gemini_api_key or s.groq_api_key or s.deepseek_api_key):
        print("No LLM API key found. Copy .env.example to .env and set at least one key.", file=sys.stderr)
        return 1
    async with httpx.AsyncClient(follow_redirects=True, headers={"User-Agent": UA}) as client:
        if args.file:
            html = Path(args.file).read_text(encoding="utf-8", errors="ignore")
        else:
            r = await client.get(args.url, timeout=30)
            r.raise_for_status()
            html = r.text
        text = html_to_text(html)
        orch = LLMOrchestrator(build_chain(s, client), s)
        try:
            res = await orch.extract(text, SCHEMAS[args.type], source_url=args.url)
        except NoEntityFoundError as exc:
            print(f"no {args.type} found on page: {exc}")
            return 2
        except AllProvidersFailedError as exc:
            print(f"FAILED: {exc}", file=sys.stderr)
            return 1
        print(json.dumps({
            "provider": res.provider,
            "model": res.model,
            "tokens_sent": res.chunk.tokens,
            "tokens_original": res.chunk.original_tokens,
            "truncated": res.chunk.truncated,
            "dropped_fields": res.dropped_fields,
            "attempts": [a.outcome for a in res.attempts],
            "data": res.data.model_dump(mode="json"),
        }, indent=2, ensure_ascii=False))
        return 0


async def _papers(args: argparse.Namespace) -> int:
    from datetime import date

    from .pipeline.papers import PaperRunConfig, run

    cfg = PaperRunConfig(
        target_with_code=args.target,
        hf_start=date.fromisoformat(args.hf_start) if args.hf_start else None,
        hf_max_days=args.hf_max_days,
        arxiv_days=args.arxiv_days,
        hf_link_lookups=args.hf_lookups,
        pwc_pages=args.pwc_pages,
        pwc_urls=tuple(args.pwc_url or ()),
    )
    summary = await run(cfg)
    print(json.dumps(summary, indent=2))
    return 0


async def _signals(args: argparse.Namespace) -> int:
    from .pipeline.signals import run

    summary = await run(
        news=not args.jobs_only,
        jobs=not args.news_only,
        only=set(args.source) if args.source else None,
    )
    print(json.dumps(summary, indent=2))
    return 0


async def _catalog(args: argparse.Namespace) -> int:
    from .pipeline import catalog

    if args.cmd == "startups":
        summary = await catalog.run_startups(limit=args.limit, refresh=args.refresh)
    elif getattr(args, "retry_makers", False):
        summary = await catalog.retry_makers(limit=args.limit)
    else:
        summary = await catalog.run_products(limit=args.limit, refresh=args.refresh,
                                             sources=set(args.source) if args.source else None)
    print(json.dumps(summary, indent=2, default=str))
    return 0


async def _export(args: argparse.Namespace) -> int:
    from .core.http import HttpFetcher
    from .export import sheets

    s = get_settings()
    out: dict = {"xlsx": sheets.write_xlsx(s.output_dir, args.xlsx)}
    if args.push:
        sheet_id = args.sheet_id or s.google_sheet_id
        key = args.credentials or s.google_service_account_file
        if not sheet_id or not key:
            print("--push needs a sheet id (FA_GOOGLE_SHEET_ID / --sheet-id) and a service-account key "
                  "(FA_GOOGLE_SERVICE_ACCOUNT_FILE / --credentials). See README > Google Sheets.", file=sys.stderr)
            return 1
        if not Path(key).exists():
            print(f"service-account key not found: {key}", file=sys.stderr)
            return 1
        async with HttpFetcher(s) as http:
            out["sheet"] = await sheets.push_to_sheet(sheet_id, s.output_dir, http, sheets.service_account_token(key))
    print(json.dumps(out, indent=2))
    return 0


async def _resolve(args: argparse.Namespace) -> int:
    from .pipeline.resolve import run

    print(json.dumps(await run(), indent=2))
    return 0


def main() -> None:
    s = get_settings()
    configure_logging(s.log_level, s.log_json)
    ap = argparse.ArgumentParser(prog="frontier_atlas")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pp = sub.add_parser("papers", help="bulk-collect research papers with GitHub metrics")
    pp.add_argument("--target", type=int, default=1000, help="stop HF crawl after this many papers with code")
    pp.add_argument("--hf-start", help="YYYY-MM-DD, default today (walks backwards)")
    pp.add_argument("--hf-max-days", type=int, default=120)
    pp.add_argument("--arxiv-days", type=int, default=0, help="also pull N days of arXiv AI categories")
    pp.add_argument("--hf-lookups", type=int, default=0, help="HF per-paper lookups for arXiv-only papers")
    pp.add_argument("--pwc-pages", type=int, default=0, help="paperswithcode.co pages from its sitemaps")
    pp.add_argument("--pwc-url", action="append", help="specific paperswithcode.co paper URL (repeatable)")

    sg = sub.add_parser("signals", help="crawl 5 news sources (+1 protected) and 5 job boards, keep only <24h items")
    g = sg.add_mutually_exclusive_group()
    g.add_argument("--news-only", action="store_true")
    g.add_argument("--jobs-only", action="store_true")
    sg.add_argument("--source", action="append", help="limit to source ids, e.g. techcrunch-ai, himalayas")
    ex = sub.add_parser("extract", help="run the LLM extractor on one page")
    ex.add_argument("--type", choices=SCHEMAS, required=True)
    src = ex.add_mutually_exclusive_group(required=True)
    src.add_argument("--url")
    src.add_argument("--file")
    for name, hlp in (("startups", "bulk-crawl AI startups (YC directory)"),
                      ("products", "bulk-crawl AI products (Futurepedia + OpenRouter)")):
        c = sub.add_parser(name, help=hlp)
        c.add_argument("--limit", type=int, help="max new items to fetch this run")
        c.add_argument("--refresh", action="store_true", help="re-fetch items already stored")
        if name == "products":
            c.add_argument("--source", action="append", choices=["futurepedia", "openrouter"])
            c.add_argument("--retry-makers", action="store_true",
                           help="re-try maker lookup for products without one, via the protected fetcher")
    exp = sub.add_parser("export", help="6-tab workbook (xlsx) for Google Sheets; --push writes the Google Sheet")
    exp.add_argument("--xlsx", help="output path (default data/output/frontier_atlas.xlsx)")
    exp.add_argument("--push", action="store_true", help="also write the tabs into the Google Sheet via the API")
    exp.add_argument("--sheet-id", help="overrides FA_GOOGLE_SHEET_ID")
    exp.add_argument("--credentials", help="service-account JSON; overrides FA_GOOGLE_SERVICE_ACCOUNT_FILE")
    sub.add_parser("resolve", help="entity resolution over all stored records + mapping log")

    args = ap.parse_args()
    handler = {"extract": _extract, "papers": _papers, "signals": _signals, "resolve": _resolve,
               "startups": _catalog, "products": _catalog, "export": _export}[args.cmd]
    raise SystemExit(asyncio.run(handler(args)))


if __name__ == "__main__":
    main()
