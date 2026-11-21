from __future__ import annotations

import html as htmllib
import json

import httpx
import pytest

from frontier_atlas.core.http import FetchError, HttpFetcher
from frontier_atlas.crawlers.bulk import products as P
from frontier_atlas.crawlers.bulk import startups as S
from frontier_atlas.crawlers.bulk.makers import (
    from_copyright, is_platform, maker_from_html, registrable_domain,
)
from frontier_atlas.pipeline.bulk import BulkSource, crawl
from frontier_atlas.pipeline.resolve import run_resolution
from frontier_atlas.schemas import PricingModel, ProductContent, ProductRecord, Source
from frontier_atlas.storage.db import RecordStore

from .conftest import make_settings


# ---------------------------------------------------------------- YC startups
def yc_page(company: dict) -> str:
    data = {"component": "Companies/ShowPage", "props": {"company": company, "launches": []}}
    return f'<html><body><div id="app" data-page="{htmllib.escape(json.dumps(data))}"></div></body></html>'


YC = {
    "slug": "acme-ai", "name": "Acme AI", "batch_name": "Winter 2024", "one_liner": "LLM agents for invoices",
    "website": "https://acme.ai", "tags": ["Artificial Intelligence", "Fintech"], "ycdc_status": "Active",
    "year_founded": 2023, "team_size": 12, "location": "San Francisco", "country": "US",
    "linkedin_url": "https://www.linkedin.com/company/acme-ai/", "founders": [{"full_name": "Ada L"}, {"full_name": "Bo K"}],
}


def test_parse_yc_company_page():
    c = S.parse_company_page(yc_page(YC))
    rec = S.to_record(c, "https://www.ycombinator.com/companies/acme-ai")
    d = rec.content.data
    assert rec.content.entityName == "Acme AI" and rec.content.rawName == "Acme AI"
    assert (d.employeeCount, d.foundedYear, d.batch, d.status) == (12, 2023, "Winter 2024", "Active")
    assert d.location == "San Francisco, US" and d.founders == ["Ada L", "Bo K"]
    assert str(rec.source.url) == "https://www.ycombinator.com/companies/acme-ai"


def test_yc_team_size_zero_is_unknown_and_ai_recheck():
    c = dict(YC, team_size=0, tags=["Fintech"], one_liner="Payroll for restaurants", long_description="")
    assert S.to_record(c, "https://www.ycombinator.com/companies/x").content.data.employeeCount is None
    assert not S.page_says_ai(c)
    assert S.page_says_ai(dict(c, one_liner="AI-native payroll"))
    assert S.parse_company_page("<html>no data</html>") is None


def _fetcher(handler) -> HttpFetcher:
    return HttpFetcher(make_settings(http_max_retries=0), client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_yc_fetch_404_yields_nothing():
    def h(req):
        return httpx.Response(404) if "gone" in str(req.url) else httpx.Response(200, text=yc_page(YC))
    async with _fetcher(h) as http:
        assert await S.fetch_company(http, "gone") == []
        [(key, rec)] = await S.fetch_company(http, "acme-ai")
    assert key == "startup:yc:acme-ai" and rec.content.entityName == "Acme AI"


# ---------------------------------------------------------------- pricing
@pytest.mark.parametrize("labels,expected", [
    (["Freemium"], PricingModel.FREEMIUM),
    (["Free"], PricingModel.FREE),
    (["Paid"], PricingModel.PAID),
    (["Free Trial"], PricingModel.PAID),            # a trial is not a free tier
    (["Free Trial", "Paid"], PricingModel.PAID),
    (["Free", "Paid"], PricingModel.FREEMIUM),
    (["Contact for Pricing"], PricingModel.ENTERPRISE),
    (["Free", "Contact for Pricing"], PricingModel.FREEMIUM),
    ([], None),
    (["Something odd"], None),
])
def test_map_pricing(labels, expected):
    assert P.map_pricing(labels) == expected


# ---------------------------------------------------------------- Futurepedia page
def fp_page(slug: str, tool: dict, summary_pricing: str = "Freemium") -> str:
    obj = {"toolId": "t-1", "slug": slug, "toolName": tool["name"], "websiteUrl": tool["web"],
           "pricing": tool["pricing"], "toolCategories": [
               {"categoryName": "storyteller", "parentCategory": {"parentCategoryName": "AI Text Generators"}}]}
    flight = f'58:["$","$L76",null,{json.dumps(obj)}]\n'
    return (f'<html><head><meta name="description" content="Write stories with AI."></head><body>'
            f'<dl><dt>Pricing</dt><dd>{summary_pricing}</dd></dl>'
            f'<script>self.__next_f.push([1,{json.dumps(flight)}])</script></body></html>')


def test_fp_parse_uses_structured_pricing_not_summary_box():
    page = fp_page("storycraft", {"name": "Storycraft", "web": "https://www.storycraft.gg/?utm_source=futurepedia",
                                  "pricing": ["Contact for Pricing"]}, summary_pricing="Freemium")
    t = P.fp_parse(page, "https://www.futurepedia.io/tool/storycraft")
    assert t.name == "Storycraft" and t.website == "https://www.storycraft.gg/"      # utm stripped
    assert t.pricing == ["Contact for Pricing"] and P.map_pricing(t.pricing) == PricingModel.ENTERPRISE
    assert t.categories == ["AI Text Generators", "storyteller"] and t.description == "Write stories with AI."


def test_fp_parse_ignores_other_tools_on_page():
    page = fp_page("other-tool", {"name": "Other", "web": "https://o.ai", "pricing": ["Free"]})
    assert P.fp_parse(page, "https://www.futurepedia.io/tool/storycraft") is None


# ---------------------------------------------------------------- OpenRouter
def _m(mid, name, prompt, completion):
    return {"id": mid, "name": name, "description": "A model. More text.", "architecture": {"modality": "text->text"},
            "pricing": {"prompt": prompt, "completion": completion}}


def test_openrouter_grouping_and_pricing():
    recs = dict(P.or_products([
        _m("meta-llama/llama-3.3-70b", "Meta: Llama 3.3 70B", "0.0000001", "0.0000003"),
        _m("meta-llama/llama-3.3-70b:free", "Meta: Llama 3.3 70B (free)", "0", "0"),
        _m("acme/tiny:free", "Acme: Tiny (free)", "0", "0"),
        _m("mistralai/mistral-large", "Mistral Large", "0.000002", "0.000006"),
        _m("openrouter/auto", "Auto Router", "-1", "-1"),
    ]))
    llama = recs["product:openrouter:meta-llama/llama-3.3-70b"].content
    assert (llama.productName, llama.startupName, llama.pricingModel) == ("Llama 3.3 70B", "Meta", PricingModel.FREEMIUM)
    assert recs["product:openrouter:acme/tiny"].content.pricingModel == PricingModel.FREE
    ml = recs["product:openrouter:mistralai/mistral-large"].content
    assert (ml.rawStartupName, ml.startupNameSource, ml.pricingModel) == ("mistralai", "source:model-id-author", PricingModel.PAID)
    auto = recs["product:openrouter:openrouter/auto"].content
    assert auto.pricingModel is None and auto.pricingRaw.startswith("variable")
    assert llama.description == "A model."
    assert len(recs) == 4


# ---------------------------------------------------------------- maker lookup
@pytest.mark.parametrize("text,expected", [
    ("Terms © Copyright 2026 Log10, Inc. All rights reserved. Platform", "Log10, Inc"),
    ("Payment © 2023- 2026 Copyright by MimicPC© All rights reserved", "MimicPC"),
    ("Copyright © 2019-2025 Blackmagic Design Pty. Ltd. All rights reserved.", "Blackmagic Design Pty. Ltd"),
    ("© 2025 Acme Labs Inc. | Privacy", "Acme Labs Inc"),
    ("(c) 2024 starryai", "starryai"),
    ("Made with love. © 2026", None),
    ("© 2026 All rights reserved.", None),
])
def test_copyright(text, expected):
    assert from_copyright(text) == expected


def test_maker_priority_and_scripts_ignored():
    org = '<script type="application/ld+json">{"@type":"Organization","name":"Acme Labs"}</script>'
    foot = "<footer>© 2025 Footer Corp. All rights reserved.</footer>"
    og = '<meta property="og:site_name" content="AcmeSite">'
    m = maker_from_html(f"<html><head>{org}{og}</head><body>{foot}</body></html>", "https://acme.ai/")
    assert (m.name, m.method) == ("Acme Labs", "website:json-ld")
    m = maker_from_html(f"<html><head>{og}</head><body>{foot}</body></html>", "https://acme.ai/")
    assert (m.name, m.method) == ("Footer Corp", "website:copyright")
    # a copyright string inside a script (FAQ JSON) must not count
    js = '<script>var faq="What is the copyright for using Acme images?"</script>'
    m = maker_from_html(f"<html><head>{og}</head><body>{js}</body></html>", "https://acme.ai/")
    assert (m.name, m.method) == ("AcmeSite", "website:og:site_name")


def test_title_only_when_it_is_the_domain():
    page = "<html><head><title>Gencraft: AI art generator</title></head><body></body></html>"
    assert maker_from_html(page, "https://gencraft.com/").name == "Gencraft"
    assert maker_from_html(page, "https://other.com/") is None
    assert maker_from_html("<html><body>© 2025 Webflow</body></html>", "https://x.io/") is None   # builder, not maker


def test_domains_and_platforms():
    assert registrable_domain("https://app.acme.co.uk/x") == "acme.co.uk"
    assert registrable_domain("https://www.acme.ai") == "acme.ai"
    assert is_platform("https://chromewebstore.google.com/detail/x") and is_platform("https://github.com/a/b")
    assert not is_platform("https://acme.ai/")


async def test_maker_lookup_prefers_known_startup_domain_and_caches():
    calls = []

    def h(req):
        calls.append(str(req.url))
        return httpx.Response(200, text='<html><body>© 2025 Vendor Inc.</body></html>')
    async with _fetcher(h) as http:
        ml = P.MakerLookup(http, {"acme.ai": "Acme AI"})
        assert (await ml.find("https://app.acme.ai/product")).method == "domain:yc-startup"
        assert (await ml.find("https://vendor.io/?utm_source=x")).name == "Vendor Inc"
        await ml.find("https://vendor.io/")
        assert await ml.find("https://github.com/x/y") is None
    assert calls == ["https://vendor.io/"]


# ---------------------------------------------------------------- bulk engine
async def test_bulk_crawl_resumes_and_isolates_failures(tmp_path):
    store = RecordStore(tmp_path / "db.sqlite3")

    def rec(i):
        return ProductRecord(source=Source(name="T", url=f"https://t.example/{i}"),
                             content=ProductContent(productName=f"P{i}"))

    async def fetch(i):
        if i == 3:
            raise FetchError(f"https://t.example/{i}", 500, "boom")
        if i == 4:
            raise ValueError("bad page")
        return [] if i == 5 else [(f"product:t:{i}", rec(i))]

    async def discover():
        return list(range(10))

    src = BulkSource("t", discover, fetch, key_of=lambda i: f"product:t:{i}", concurrency=3)
    st = await crawl(src, store, batch_size=2, existing=store.existing_keys("PRODUCT"))
    assert (st.stored, st.failed, st.empty) == (7, 2, 1)
    assert st.reasons == {"http_500": 1, "ValueError": 1}
    st2 = await crawl(src, store, existing=store.existing_keys("PRODUCT"))
    assert st2.skipped_existing == 7 and st2.stored == 0            # only the 3 unfinished items retried
    store.close()


# ---------------------------------------------------------------- resolution with product evidence
async def test_product_makers_resolve_with_startups(tmp_path):
    store = RecordStore(tmp_path / "db.sqlite3")

    def prod(key, name, maker, method, web):
        return (key, ProductRecord(source=Source(name="FP", url=f"https://fp.example/{key}"),
                                   content=ProductContent(productName=name, startupName=maker, rawStartupName=maker,
                                                          startupNameSource=method, website=web)))
    await store.upsert([
        prod("product:a", "ChatThing", "OpenAI OpCo, LLC", "website:copyright", "https://openai.com/"),
        prod("product:b", "Space Thing", "Some Wrapper", "website:og:site_name", "https://huggingface.co/spaces/x"),
        prod("product:c", "Llama", "meta-llama", "source:model-id-author", "https://openrouter.ai/meta-llama/x"),
    ])
    await run_resolution(store, tmp_path)
    got = {k: b["content"]["startupName"] for k, b in store.iter_items("PRODUCT")}
    assert got["product:a"] == "OpenAI"
    assert got["product:b"] == "Some Wrapper"        # platform URL is never domain evidence
    store.close()


@pytest.mark.parametrize("raw,expected", [
    ("RivalFlow, Inc Terms Privacy", "RivalFlow, Inc"),
    ("Animato Terms of Service Privacy Policy", "Animato"),
    ("PicSo.ai About Us Community Terms Contact Us", "PicSo.ai"),
    ("Coyotiv GmbH Support", "Coyotiv GmbH"),
    ("Playful Software 2026 Privacy Policy", "Playful Software"),
    ("Responsum Inc Cookie Settings", "Responsum Inc"),
    ("Climate Policy Radar", "Climate Policy Radar"),          # real names survive
    ("Made With Intent Limited", "Made With Intent Limited"),
    ("Eve Legal", "Eve Legal"),
    ("Video To Blog", "Video To Blog"),
    ("Stability AI Ltd, 2026", "Stability AI Ltd"),
    ("Privacy Policy Impressum Support", None),
    ("Manage Cookies English United States", None),
    ("Made with Framer", None),
    ("2026 杭州深度求索人工智能基础技术研究有限公司 版权所有 中文 EN",
     "杭州深度求索人工智能基础技术研究有限公司"),
])
def test_footer_link_text_trimmed(raw, expected):
    from frontier_atlas.crawlers.bulk.makers import _plausible
    assert _plausible(raw) == expected


def test_footer_boilerplate_and_platform_names_rejected():
    assert from_copyright("© 2025 Privacy Policy Terms of Service") is None
    page = '<html><head><meta property="og:site_name" content="Google Docs"></head><body></body></html>'
    assert maker_from_html(page, "https://powder.gg/") is None
