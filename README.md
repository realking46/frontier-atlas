# FrontierAtlas ingestion pipeline

An async, fault-tolerant pipeline that collects AI **startups, products, research papers, jobs and
news**, normalises them into one canonical schema, and resolves company names to canonical
entities. Built for the GraphOne / FrontierAtlas AI Engineer assessment.

- **Design document:** [`architecture.pdf`](architecture.pdf) (3 pages: scale, 413/429 handling,
  freshness across nodes, storage). Source: `docs/architecture.html`; rebuild with
  `python docs/build_pdf.py` (needs the Playwright extra).
- **Anti-bot strategy:** [`docs/anti_bot.md`](docs/anti_bot.md)

Every record carries `source.url`, the page or API its values were read from. Nothing is
inferred without a source: structured sources are parsed exactly, and LLM output must pass a
grounding check against the page text (see "No hallucinated data" below).

## Output (one CSV per Google Sheets tab)

| Tab | File | Rows | Sources |
|---|---|---|---|
| Startups | `data/output/startups.csv` | 2,273 | Y Combinator company pages (AI-tagged) |
| Products | `data/output/products.csv` | 2,961 | Futurepedia (2,491), OpenRouter (470) |
| Research Papers | `data/output/research_papers.csv` | 1,138 with code, 1,134 with live GitHub stars | HF Daily Papers, arXiv API, paperswithcode.co, GitHub GraphQL |
| Jobs | `data/output/jobs.csv` | only items < 24 h old at export time | Himalayas, Jobicy, We Work Remotely, Arbeitnow, HN Jobs |
| News | `data/output/news.csv` | only items < 24 h old at export time | TechCrunch, The Verge, MIT Tech Review, The Decoder, WIRED (+ Ars Technica, protected) |
| Entity Mapping Log | `data/output/entity_mapping_log.csv` | ~4.6k | raw name → canonical name, method, score, example URL |

Row counts are from the runs on 2026-11-21. Jobs and news are a rolling 24-hour window, so
re-export them right before submitting (`signals`).

## Setup

Requires Python 3.11+ (developed on 3.12, Windows and Linux).

```bash
python -m venv .venv
```
```bash
.venv/Scripts/python -m pip install -r requirements.txt
```
```bash
.venv/Scripts/python -m pip install -e .
```
The second step installs the `frontier-atlas` command into the venv.
(On Linux/macOS use `.venv/bin/...` instead of `.venv/Scripts/...`; the commands are the same in PowerShell, cmd and bash.) The browser tier is optional; to enable it:
```bash
.venv/Scripts/python -m playwright install chromium
```

Configuration is environment-based: copy `.env.example` to `.env` and fill in what you have.

| Variable | Needed for | Without it |
|---|---|---|
| `GITHUB_TOKEN` (no scopes) | live GitHub stars for 1k+ repos (GraphQL, 100 per call) | 60 repos/hour anonymous; rest fall back to the star count HF reports |
| `GEMINI_API_KEY`, `GROQ_API_KEY`, `DEEPSEEK_API_KEY` | LLM fallback chain (pages without structured data) | providers without a key are skipped; structured sources need no LLM |

All tunables are `FA_*` variables (see `src/frontier_atlas/core/config.py`): rate limits per
provider, retry budgets, freshness window, politeness intervals, circuit-breaker settings.

## Running

All commands are subcommands of one CLI. Run them from the repo root (paths such as `data/` and `.env` are relative to it):

```bash
.venv/Scripts/frontier-atlas startups
```
```bash
.venv/Scripts/frontier-atlas products
```
```bash
.venv/Scripts/frontier-atlas papers --target 1000
```
```bash
.venv/Scripts/frontier-atlas signals
```

| Command | What it does |
|---|---|
| `startups [--limit N] [--refresh]` | YC directory → STARTUP records. Resumable: stored keys are skipped. |
| `products [--source futurepedia\|openrouter] [--retry-makers]` | Product directories, plus a maker lookup on each product's own website. `--retry-makers` re-tries products with no maker through the protected fetcher. |
| `papers --target N [--arxiv-days D] [--pwc-pages P]` | Harvest papers, link repos, fetch live stars. |
| `signals [--news-only\|--jobs-only] [--source ID]` | News + jobs, keeps only items proven < 24 h old. Run every 1–2 h. |
| `resolve` | Entity resolution over everything stored; rewrites the mapping log and CSVs. |
| `export [--push]` | Writes the 6-tab workbook `data/output/frontier_atlas.xlsx`; with `--push`, also writes the tabs into the Google Sheet. |
| `extract --type startup\|product\|job\|news --url URL` | Run the LLM extraction engine on one page (needs an LLM key). |

Every crawl command ends by running entity resolution and re-exporting the CSVs. Run summaries
(JSON) include per-source counts, HTTP stats, and per-host anti-bot outcomes.

Tests (249, all offline with mocked network and fake LLM providers):
```bash
.venv/Scripts/python -m pytest -q
```

## Google Sheets

The six tabs (Startups, Products, Research Papers, Jobs, News, Entity Mapping Log) are built
from the exported CSVs, so the sheet always matches them. Number and boolean columns are typed,
headers are frozen with filters, text over the cell limit is cut with a pointer to the record's
`source.url`, and scraped text is always written as text, never as a formula.

**Option A: import the workbook (no credentials).**
```bash
.venv/Scripts/frontier-atlas export
```
In Google Sheets: File → Import → Upload `data/output/frontier_atlas.xlsx` → "Replace
spreadsheet". Then Share → General access → "Anyone with the link" → Viewer.

**Option B: push through the Sheets API (repeatable after every crawl).**
1. In Google Cloud: create a project, enable the **Google Sheets API**, create a **service
   account**, and download its JSON key to `secrets/service-account.json` (`secrets/` is
   gitignored).
2. Create the Google Sheet and share it with the service account's e-mail as **Editor**.
3. Set `FA_GOOGLE_SHEET_ID` (the id in the sheet's URL) and `FA_GOOGLE_SERVICE_ACCOUNT_FILE` in
   `.env`, then:
```bash
.venv/Scripts/frontier-atlas export --push
```
The push creates missing tabs and clears and rewrites only these six. It removes the new
sheet's default "Sheet1" only if that tab is empty, and leaves any other tab alone. Writes are
paced under the API's 60-requests-per-minute quota, with backoff on 429.

## Architecture at a glance

```
 sources (APIs, feeds, sitemaps, pages)
        │
        ▼
 HttpFetcher ── per-host politeness, 429/5xx backoff + jitter, Retry-After, rate-limit headers
        │  └── ProtectedFetcher ── robots.txt ▸ HTTP ▸ Chromium tier ▸ stop at CAPTCHA (circuit breaker)
        ▼
 parsers (structured first) ──► LLM orchestrator only when there is no structured data
        │                        Gemini Flash → Groq Llama 3.3 → DeepSeek,
        │                        chunker (never 413), token buckets (rarely 429), grounding gate
        ▼
 freshness gate (news/jobs) ── date parser, 3-valued verdict, listing heuristics,
        │                       SeenStore: canonical URL, atomic lease claim, SimHash near-dup
        ▼
 RecordStore (SQLite, keyed upsert; maps 1:1 to Postgres) ──► entity resolution ──► CSV / Sheets
```

| Package | Responsibility |
|---|---|
| `core/` | config, logging, error taxonomy, retry/backoff, token buckets, `HttpFetcher`, anti-bot (`antibot`, `protected`, `browser`, `robots`) |
| `extraction/` | chunker, providers (Gemini, Groq, DeepSeek over raw HTTP), orchestrator, grounding gate, prompts |
| `crawlers/bulk/` | YC startups, Futurepedia/OpenRouter products, maker lookup, papers (HF, arXiv, PwC), GitHub metrics |
| `crawlers/news/`, `crawlers/jobs/` | feeds, 5 job boards, AI-relevance and role-family rules |
| `freshness/` | date parser, verdict heuristics, `SeenStore` (exactly-once across nodes), `FreshnessGate` |
| `resolution/` | name normaliser, seed resolver (50 AI startups in `data/seeds/`), news mention matcher |
| `pipeline/` | bulk crawl engine (resumable worker pool), per-domain runners, resolution pass |
| `storage/`, `export/` | record store, CSV export, Google Sheets (xlsx workbook + Sheets API push) |
| `schemas/` | pydantic models matching the brief's field names |

## No hallucinated data

- **Structured first.** Startups, products, papers, most jobs and all news dates come from APIs,
  feeds, JSON-LD or embedded page JSON. No LLM is involved for these, so there is nothing to
  hallucinate.
- **LLM output is grounded.** Fields marked `grounded` (names, titles, numbers) must appear in
  the source text (fuzzy match ≥ 85). If the identity field does not, the extraction is repaired
  once, then rejected. Ungrounded secondary fields are nulled.
- **Product makers come from evidence.** Futurepedia does not name the company behind a product,
  so the maker is read from the product's own website (schema.org Organization, copyright line,
  site name) and the evidence page is stored in `startupEvidenceUrl`. If no maker is found the
  field stays empty: 531 of 2,961 products.
- **Freshness is proven, not assumed.** A news or job item is ingested only if it is provably
  < 24 h old. Items that are possibly fresh but unproven are dropped and recorded.
- **Protected pages are labelled.** Ars Technica's article pages are behind a CAPTCHA. Its items
  carry the publisher's feed lead with `text_source=feed-lead`, never presented as full text.

## Known limitations

- The LLM path is fully tested offline (fallback, 413 shrink, 429 cooldown, repair, grounding)
  but was not exercised live in these runs: structured sources covered every target.
- Cloudflare's JavaScript challenge detects headless Chromium. Those sites stay blocked by design
  (no stealth evasion); see `docs/anti_bot.md` for the alternatives used.
- The Sheets API push is covered by tests against a mocked API but needs your service account
  to run live; the xlsx import path needs no setup.
- Storage is SQLite for the trial. The interfaces (`RecordStore`, `SeenStore`, token buckets) are
  written so the production mapping to Postgres / Redis does not change callers.
