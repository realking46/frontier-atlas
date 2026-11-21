"""AI products from two directories.

Futurepedia (~2.5k AI tools)
    Frontier: sitemap_tools.xml (robots.txt points to it; /tool/* is allowed).
    Each tool page is a Next.js page whose RSC payload carries the tool object
    {toolName, websiteUrl, pricing[], toolCategories[]}; that structured field is
    used for pricing, NOT the page's prose summary box, which can disagree with it
    (e.g. summary "Freemium" vs structured "Contact for Pricing"). The raw label is
    kept in `pricingRaw`. Futurepedia does not name the maker, so it is looked up
    on the product's own website (makers.py).

OpenRouter (~450 LLM products)
    One public JSON API call. The display name is "Maker: Model", so the maker is
    explicit. Pricing is per-token: a model offered at $0 is FREE, one with both a
    `:free` variant and a paid one is FREEMIUM, otherwise PAID (usage-based).
"""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from urllib.parse import urlparse

from selectolax.lexbor import LexborHTMLParser

from ...core.http import FetchError, HttpFetcher
from ...schemas import PricingModel, ProductContent, ProductRecord, Source
from .makers import Maker, clean_url, is_platform, maker_from_html, registrable_domain

FP_SITEMAP = "https://www.futurepedia.io/sitemap_tools.xml"
FP_NAME = "Futurepedia"
OR_MODELS = "https://openrouter.ai/api/v1/models"
OR_NAME = "OpenRouter"

_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>")
_FLIGHT_RE = re.compile(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)', re.S)


# ---------------------------------------------------------------- pricing
def map_pricing(labels: list[str]) -> PricingModel | None:
    """Futurepedia labels -> spec enum. A free trial is not a free tier."""
    ls = {l.strip().lower() for l in labels if l and l.strip()}
    if not ls:
        return None
    if "freemium" in ls:
        return PricingModel.FREEMIUM
    free = "free" in ls
    paid = bool(ls & {"paid", "subscription", "one-time payment", "one time", "pay as you go", "usage based", "free trial"})
    contact = any("contact" in l for l in ls)
    if free and (paid or contact):
        return PricingModel.FREEMIUM
    if free:
        return PricingModel.FREE
    if paid:
        return PricingModel.PAID
    if contact:
        return PricingModel.ENTERPRISE
    return None


# ---------------------------------------------------------------- Futurepedia
async def fp_discover(http: HttpFetcher) -> list[str]:
    xml = await http.get_text(FP_SITEMAP)
    return list(dict.fromkeys(u for u in _LOC_RE.findall(xml) if "/tool/" in u))


def fp_slug(url: str) -> str:
    return urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]


def _flight_text(page_html: str) -> str:
    parts = []
    for m in _FLIGHT_RE.finditer(page_html):
        try:
            parts.append(json.loads(m.group(1)))
        except json.JSONDecodeError:
            continue
    return "".join(parts)


@dataclass
class FPTool:
    slug: str
    name: str
    website: str | None
    pricing: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    description: str | None = None


def fp_parse(page_html: str, url: str) -> FPTool | None:
    slug = fp_slug(url)
    flight = _flight_text(page_html)
    dec = json.JSONDecoder()
    obj = None
    for m in re.finditer(r'\{"toolId":\s*"[^"]+",\s*"slug":\s*"%s",\s*"toolName":' % re.escape(slug), flight):
        try:
            cand, _ = dec.raw_decode(flight, m.start())
        except json.JSONDecodeError:
            continue
        if "pricing" in cand or "websiteUrl" in cand:
            obj = cand
            break
    if not obj or not obj.get("toolName"):
        return None
    tree = LexborHTMLParser(page_html)
    desc_node = tree.css_first('meta[name="description"]')
    cats = []
    for c in obj.get("toolCategories") or []:
        parent = ((c.get("parentCategory") or {}).get("parentCategoryName"))
        for x in (parent, c.get("categoryName")):
            if x and x not in cats:
                cats.append(x)
    web = obj.get("websiteUrl")
    return FPTool(
        slug=slug,
        name=obj["toolName"].strip(),
        website=clean_url(web) if web else None,
        pricing=[p for p in obj.get("pricing") or [] if isinstance(p, str)],
        categories=cats,
        description=(desc_node.attributes.get("content") or "").strip() or None if desc_node else None,
    )


def product_key(source: str, ident: str) -> str:
    return f"product:{source}:{ident.lower()}"


class MakerLookup:
    """Product website -> maker, with the domain index of startups already stored."""

    def __init__(self, http: HttpFetcher, startup_domains: dict[str, str] | None = None, fetcher=None):
        self.http = http
        self.fetcher = fetcher            # optional ProtectedFetcher (robots + bot-protection tiers)
        self.startup_domains = startup_domains or {}
        self._cache: dict[str, Maker | None] = {}
        self.outcomes: Counter[str] = Counter()

    async def find(self, website: str | None) -> Maker | None:
        if not website or is_platform(website):
            return None
        dom = registrable_domain(website)
        if dom and dom in self.startup_domains:
            return Maker(self.startup_domains[dom], "domain:yc-startup", website)
        home = clean_url(website)
        if home in self._cache:
            return self._cache[home]
        try:
            if self.fetcher is not None:
                page = await self.fetcher.fetch(home, max_retries=0)   # one attempt per tier: a vendor homepage is not worth minutes of backoff
                m = maker_from_html(page.html, page.final_url)
                self.outcomes[f"fetched:{page.tier}"] += 1
            else:
                resp = await self.http.get(home, max_retries=1)
                m = maker_from_html(resp.text, str(resp.url))
                self.outcomes["fetched:http"] += 1
            self.outcomes["maker_found" if m else "no_maker_on_page"] += 1
        except FetchError as exc:
            block = getattr(exc, "block", None)
            self.outcomes[f"blocked:{block.kind.value}" if block else f"error:{exc.status or 'network'}"] += 1
            m = None
        except Exception as exc:  # vendor sites fail in every way imaginable
            self.outcomes[f"error:{type(exc).__name__}"] += 1
            m = None
        self._cache[home] = m
        return m


async def fp_fetch(http: HttpFetcher, makers: MakerLookup, url: str) -> list[tuple[str, ProductRecord]]:
    try:
        page = await http.get_text(url, max_retries=2)
    except FetchError as exc:
        if exc.status in (404, 410):
            return []
        raise
    tool = fp_parse(page, url)
    if not tool:
        return []
    maker = await makers.find(tool.website)
    rec = ProductRecord(
        source=Source(name=FP_NAME, url=url),
        content=ProductContent(
            productName=tool.name,
            startupName=maker.name if maker else None,
            rawStartupName=maker.name if maker else None,
            startupNameSource=maker.method if maker else None,
            startupEvidenceUrl=maker.evidence_url if maker else None,
            pricingModel=map_pricing(tool.pricing),
            pricingRaw="; ".join(tool.pricing) or None,
            description=tool.description,
            website=tool.website,
            categories=tool.categories,
        ),
    )
    return [(product_key("futurepedia", tool.slug), rec)]


# ---------------------------------------------------------------- OpenRouter
def _price(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def or_products(models: list[dict]) -> list[tuple[str, ProductRecord]]:
    groups: dict[str, list[dict]] = {}
    for m in models:
        mid = m.get("id") or ""
        if not mid or "/" not in mid:
            continue
        groups.setdefault(mid.removesuffix(":free"), []).append(m)
    out = []
    for base, variants in groups.items():
        main = next((v for v in variants if not v["id"].endswith(":free")), variants[0])
        name = re.sub(r"\s*\(free\)\s*$", "", main.get("name") or "").strip()
        if ": " in name:
            maker, product = name.split(": ", 1)
            method = "source:name-prefix"
        else:
            maker, product, method = base.split("/", 1)[0], name, "source:model-id-author"
        tiers = []
        for v in variants:
            p = v.get("pricing") or {}
            pr, cp = _price(p.get("prompt")), _price(p.get("completion"))
            if pr is None or cp is None or pr < 0 or cp < 0:   # router pseudo-models (-1 = dynamic)
                continue
            tiers.append(pr == 0 and cp == 0)
        if not tiers:
            pricing = None
        elif all(tiers):
            pricing = PricingModel.FREE
        elif any(tiers):
            pricing = PricingModel.FREEMIUM
        else:
            pricing = PricingModel.PAID
        p = main.get("pricing") or {}
        if tiers:
            raw = f"prompt ${p.get('prompt')}/tok, completion ${p.get('completion')}/tok"
        else:
            raw = "variable: billed at the price of the model it routes to"
        if len(variants) > 1:
            raw += "; free variant available"
        desc = (main.get("description") or "").strip()
        desc = re.split(r"(?<=[.!?])\s", desc, 1)[0] if desc else None
        url = f"https://openrouter.ai/{base}"
        out.append((product_key("openrouter", base), ProductRecord(
            source=Source(name=OR_NAME, url=url),
            content=ProductContent(
                productName=product or base,
                startupName=maker, rawStartupName=maker, startupNameSource=method, startupEvidenceUrl=OR_MODELS,
                pricingModel=pricing, pricingRaw=raw, description=desc, website=url,
                categories=["LLM", (main.get("architecture") or {}).get("modality") or ""],
            ),
        )))
    for _, r in out:
        r.content.categories = [c for c in r.content.categories if c]
    return out


async def or_fetch_all(http: HttpFetcher) -> list[tuple[str, ProductRecord]]:
    data = await http.get_json(OR_MODELS)
    return or_products(data.get("data") or [])
