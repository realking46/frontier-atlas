"""Live check of the LLM extraction engine against the real providers.

    .venv/Scripts/python scripts/llm_live_check.py            # all scenarios
    .venv/Scripts/python scripts/llm_live_check.py A B         # some of them

Scenarios
  A  full chain on real pages of every record type (startup, product, job, news)
  B  provider failure: Gemini pointed at a model id that does not exist -> real 404 -> Groq serves
  C  size: (1) chunker on a very long page; (2) Groq allowed an oversized budget on purpose -> real 413
     from the API -> engine shrinks x0.55 and retries
  D  rate limit: client-side limiter loosened on purpose and a burst sent to Groq -> real 429s ->
     backoff honouring Retry-After, cooldown, fall-through to Gemini
  E  quota exhaustion: run while a provider's daily cap is used up -> its 429 asks for hours ->
     no sleep, provider cooled down for that long, immediate fall-through

Nothing is stored. Results go to docs/llm_live_run.json (per scenario) and docs/llm_live_run.md. API keys are read from .env and never
printed (they travel in request headers, not URLs).
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from frontier_atlas.core.config import Settings  # noqa: E402
from frontier_atlas.core.errors import AllProvidersFailedError, NoEntityFoundError  # noqa: E402
from frontier_atlas.core.logging import configure_logging  # noqa: E402
from frontier_atlas.extraction import LLMOrchestrator, build_chain, html_to_text  # noqa: E402
from frontier_atlas.schemas import JobExtraction, NewsExtraction, ProductExtraction, StartupExtraction  # noqa: E402

UA = "FrontierAtlasBot/0.1 (+research data pipeline)"
PAGES_A = [
    ("startup", StartupExtraction, "https://www.greptile.com/"),
    ("startup", StartupExtraction, "https://www.trychroma.com/"),
    ("product", ProductExtraction, "https://www.xano.com/"),
    ("job", JobExtraction, "https://www.arbeitnow.nl/jobs/companies/luke-recruitment/senior-social-ai-marketeer-tilburg-68538"),
    ("news", NewsExtraction, "https://techcrunch.com/2026/11/21/all-the-ai-agents-that-can-live-in-your-text-messages/"),
]
LONG_PAGE = "https://arxiv.org/html/2402.06196"   # survey paper, ~54.5k tokens of text
BURST_PAGES = [
    "https://www.greptile.com/", "https://www.trychroma.com/", "https://www.xano.com/",
    "https://modal.com/", "https://www.reducto.ai/", "https://synthflow.ai/",
]
OUT = ROOT / "docs" / "llm_live_run.md"
STORE = ROOT / "docs" / "llm_live_run.json"     # per-scenario results; the .md is rendered from it


def settings(**over) -> Settings:
    return Settings(_env_file=str(ROOT / ".env"), **over)


async def fetch(client: httpx.AsyncClient, url: str) -> str | None:
    try:
        r = await client.get(url, timeout=30)
        return r.text if r.status_code == 200 else None
    except httpx.HTTPError:
        return None


def summarize(res, elapsed: float) -> dict:
    data = res.data.model_dump(mode="json")
    return {
        "provider": f"{res.provider} ({res.model})",
        "attempts": " → ".join(f"{a.provider}:{a.outcome}" + (f"[{a.input_tokens}t]" if a.input_tokens else "")
                               for a in res.attempts),
        "tokens": f"{res.chunk.tokens}/{res.chunk.original_tokens}" + (" truncated" if res.chunk.truncated else ""),
        "dropped": ", ".join(res.dropped_fields) or "none",
        "data": {k: v for k, v in data.items() if v not in (None, [], "")},
        "seconds": round(elapsed, 1),
    }


async def run_one(orch, client, kind, schema, url) -> dict:
    html = await fetch(client, url)
    if html is None:
        return {"url": url, "kind": kind, "error": "page not fetchable"}
    text = html_to_text(html)
    t0 = time.monotonic()
    try:
        res = await orch.extract(text, schema, source_url=url)
    except NoEntityFoundError as exc:
        return {"url": url, "kind": kind, "error": f"no entity: {exc}"[:200]}
    except AllProvidersFailedError as exc:
        return {"url": url, "kind": kind, "error": f"all providers failed: {exc}"[:400]}
    return {"url": url, "kind": kind, **summarize(res, time.monotonic() - t0)}


async def scenario_a(client) -> dict:
    s = settings()
    orch = LLMOrchestrator(build_chain(s, client), s)
    rows = [await run_one(orch, client, k, sch, u) for k, sch, u in PAGES_A]
    return {"title": "A. Full chain on real pages", "settings": f"chain={s.llm_chain}", "rows": rows,
            "stats": dict(orch.stats)}


async def scenario_b(client) -> dict:
    s = settings(gemini_model="gemini-0-does-not-exist")
    orch = LLMOrchestrator(build_chain(s, client), s)
    row = await run_one(orch, client, "startup", StartupExtraction, "https://www.greptile.com/")
    row2 = await run_one(orch, client, "product", ProductExtraction, "https://www.xano.com/")
    return {"title": "B. Provider failure → fall-through", "settings": "gemini_model=gemini-0-does-not-exist",
            "rows": [row, row2], "stats": dict(orch.stats)}


async def scenario_c(client) -> dict:
    s1 = settings()
    orch1 = LLMOrchestrator(build_chain(s1, client), s1)
    r1 = await run_one(orch1, client, "news", NewsExtraction, LONG_PAGE)
    # Allow Groq a budget far above its real 8k TPM per request, disable the client limiter
    s2 = settings(llm_chain="groq", groq_max_input_tokens=20_000, groq_tpm=10**9)
    orch2 = LLMOrchestrator(build_chain(s2, client), s2)
    r2 = await run_one(orch2, client, "news", NewsExtraction, LONG_PAGE)
    return {"title": "C. Oversized pages: chunker, then a real 413", "settings":
            "(1) default budgets; (2) chain=groq, groq_max_input_tokens=20000 (real limit 8000), limiter off",
            "rows": [r1, r2], "stats": {**{f"1.{k}": v for k, v in orch1.stats.items()},
                                        **{f"2.{k}": v for k, v in orch2.stats.items()}}}


async def scenario_d(client) -> dict:
    s = settings(llm_chain="groq,gemini", groq_tpm=10**9, groq_rpm=10_000, llm_max_429_retries=1)
    orch = LLMOrchestrator(build_chain(s, client), s)
    pages = []
    for u in BURST_PAGES:
        if (h := await fetch(client, u)):
            pages.append((u, html_to_text(h)))

    async def one(u, text):
        t0 = time.monotonic()
        try:
            res = await orch.extract(text, StartupExtraction, source_url=u)
            return {"url": u, "kind": "startup", **summarize(res, time.monotonic() - t0)}
        except (NoEntityFoundError, AllProvidersFailedError) as exc:
            return {"url": u, "kind": "startup", "error": str(exc)[:300]}
    rows = await asyncio.gather(*(one(u, t) for u, t in pages))
    return {"title": "D. Burst over Groq's 8k tokens/min → real 429s", "settings":
            "chain=groq,gemini; client limiter off for Groq; llm_max_429_retries=1; 6 concurrent requests",
            "rows": rows, "stats": dict(orch.stats)}


async def scenario_e(client) -> dict:
    """Default chain while a provider is out of quota (run when Gemini's free daily cap is used up)."""
    s = settings()
    orch = LLMOrchestrator(build_chain(s, client), s)
    rows = [await run_one(orch, client, "startup", StartupExtraction, "https://www.greptile.com/"),
            await run_one(orch, client, "product", ProductExtraction, "https://www.xano.com/")]
    return {"title": "E. Provider out of daily quota → immediate fall-through", "settings":
            f"chain={s.llm_chain}; Gemini free-tier daily cap exhausted; llm_max_retry_wait_s={s.llm_max_retry_wait_s}",
            "rows": rows, "stats": dict(orch.stats)}


def section_md(r: dict, run_at: str) -> str:
    lines = [f"## {r['title']}", "", f"Run {run_at}. Settings: `{r['settings']}`", "",
             "| Page | Served by | Attempts | Tokens | Dropped | Time |", "|---|---|---|---|---|---|"]
    for row in r["rows"]:
        if "error" in row:
            lines.append(f"| {row['kind']}: {row['url']} | — | {row['error']} | | | |")
        else:
            lines.append(f"| {row['kind']}: {row['url']} | {row['provider']} | {row['attempts']} | "
                         f"{row['tokens']} | {row['dropped']} | {row['seconds']} s |")
    lines += ["", "<details><summary>Extracted data</summary>", "", "```json",
              json.dumps({row["url"]: row.get("data") for row in r["rows"] if "data" in row}, indent=1, ensure_ascii=False),
              "```", "", "</details>", "", f"Orchestrator counters: `{json.dumps(r['stats'])}`", ""]
    return "\n".join(lines)


def render(store: dict) -> str:
    s = settings()
    head = [
        "# LLM engine: live runs",
        "",
        f"Produced by `scripts/llm_live_check.py` against the real APIs. Models: Gemini `{s.gemini_model}` "
        f"(thinking level `{s.gemini_thinking_level}`), Groq `{s.groq_model}`; DeepSeek had no key and was "
        "skipped by the chain. Each scenario keeps the time it was last run. Nothing was stored.",
        "",
        "Columns: **attempts** lists each provider call in order (`provider:outcome[input tokens]`); "
        "**tokens** is sent/original after chunking; **dropped** lists fields the grounding gate "
        "nulled because the value was not in the page text.",
        "",
    ]
    return "\n".join(head + [store[k]["md"] for k in sorted(store)])


async def main(which: set[str]) -> None:
    configure_logging("INFO", False)
    fns = {"A": scenario_a, "B": scenario_b, "C": scenario_c, "D": scenario_d, "E": scenario_e}
    store = json.loads(STORE.read_text(encoding="utf-8")) if STORE.exists() else {}
    async with httpx.AsyncClient(follow_redirects=True, headers={"User-Agent": UA}) as client:
        for k in sorted(which):
            print(f"--- scenario {k}", flush=True)
            r = await fns[k](client)
            run_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            store[k] = {"run_at": run_at, "md": section_md(r, run_at)}
            STORE.write_text(json.dumps(store, indent=1, ensure_ascii=False), encoding="utf-8")
            OUT.write_text(render(store), encoding="utf-8")       # written after every scenario
    print(f"report: {OUT}")


if __name__ == "__main__":
    asyncio.run(main(set(sys.argv[1:]) or {"A", "B", "C", "D", "E"}))
