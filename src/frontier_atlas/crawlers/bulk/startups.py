"""AI startups from the Y Combinator company directory.

Discovery: YC's directory listing is rendered client-side from a search index
(and `/companies?*` is disallowed in robots.txt), so the frontier of company
slugs comes from the public yc-oss mirror of that index. The mirror is used
ONLY as a list of slugs to visit: every field in a record is read from the
company's own page on ycombinator.com, which embeds the full company object as
JSON (`data-page`). A slug whose page 404s (renamed / hidden company) yields
nothing, so the mirror can never put a value into the dataset.

No LLM is needed: the page is structured, so extraction is exact.
"""
from __future__ import annotations

import html as htmllib
import json
import re

from ...core.http import FetchError, HttpFetcher
from ...schemas import Source, StartupContent, StartupData, StartupRecord

YC_INDEX_URL = "https://yc-oss.github.io/api/companies/all.json"
YC_COMPANY_URL = "https://www.ycombinator.com/companies/{slug}"
SOURCE_NAME = "Y Combinator Directory"

_DATA_PAGE_RE = re.compile(r'data-page="([^"]*)"')
AI_TAGS = {
    "artificial intelligence", "ai", "generative ai", "machine learning", "deep learning", "ai assistant",
    "aiops", "llm", "llms", "computer vision", "nlp", "natural language processing", "conversational ai",
    "ai-enhanced learning", "ai-powered drug discovery", "speech recognition",
}
AI_TEXT_RE = re.compile(
    r"\b(AI|A\.I\.|LLMs?|GPT|machine learning|deep learning|artificial intelligence|neural|"
    r"computer vision|generative|foundation models?|AI[- ](?:native|powered|first|agents?))\b"
)


def is_ai_index_entry(e: dict) -> bool:
    tags = {t.lower() for t in e.get("tags") or []}
    return bool(tags & AI_TAGS) or "artificial intelligence" in (e.get("subindustry") or "").lower()


async def discover_slugs(http: HttpFetcher) -> list[str]:
    data = await http.get_json(YC_INDEX_URL)
    slugs = [e["slug"] for e in data if e.get("slug") and is_ai_index_entry(e)]
    return list(dict.fromkeys(slugs))


def parse_company_page(page_html: str) -> dict | None:
    m = _DATA_PAGE_RE.search(page_html)
    if not m:
        return None
    try:
        data = json.loads(htmllib.unescape(m.group(1)))
    except json.JSONDecodeError:
        return None
    return (data.get("props") or {}).get("company")


def page_says_ai(c: dict) -> bool:
    """Re-check AI relevance on the page itself (the index is only a hint)."""
    if {t.lower() for t in c.get("tags") or []} & AI_TAGS:
        return True
    return bool(AI_TEXT_RE.search(f"{c.get('one_liner') or ''} {c.get('long_description') or ''}"))


def _int(v) -> int | None:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def to_record(c: dict, url: str) -> StartupRecord | None:
    name = (c.get("name") or "").strip()
    if not name:
        return None
    status = c.get("ycdc_status") or c.get("status")
    loc = ", ".join(x for x in (c.get("location") or c.get("city"), c.get("country")) if x) or None
    return StartupRecord(
        source=Source(name=SOURCE_NAME, url=url),
        content=StartupContent(
            entityName=name,
            rawName=name,
            data=StartupData(
                employeeCount=_int(c.get("team_size")),
                description=(c.get("one_liner") or "").strip() or None,
                website=(c.get("website") or "").strip() or None,
                foundedYear=_int(c.get("year_founded")),
                location=loc,
                country=c.get("country"),
                industries=[t for t in c.get("tags") or [] if t],
                status=status,
                batch=c.get("batch_name") or c.get("batch"),
                linkedin=c.get("linkedin_url"),
                founders=[f.get("full_name") for f in c.get("founders") or [] if f.get("full_name")],
            ),
        ),
    )


def startup_key(slug: str) -> str:
    return f"startup:yc:{slug}"


async def fetch_company(http: HttpFetcher, slug: str) -> list[tuple[str, StartupRecord]]:
    url = YC_COMPANY_URL.format(slug=slug)
    try:
        page = await http.get_text(url, max_retries=2)
    except FetchError as exc:
        if exc.status == 404:
            return []
        raise
    c = parse_company_page(page)
    if not c or not page_says_ai(c):
        return []
    rec = to_record(c, url)
    return [(startup_key(slug), rec)] if rec else []
