from __future__ import annotations

import csv
import random
from datetime import datetime, timezone

import pytest

from frontier_atlas.resolution.mentions import MentionMatcher
from frontier_atlas.resolution.normalizer import keys, strip_legal
from frontier_atlas.resolution.resolver import EntityResolver, load_seeds
from frontier_atlas.schemas import JobContent, JobRecord, NewsContent, NewsRecord, Source
from frontier_atlas.storage.db import RecordStore
from frontier_atlas.pipeline.resolve import run_resolution


@pytest.fixture(scope="module")
def r() -> EntityResolver:
    return EntityResolver()


def test_seed_db_has_50_unique_entities():
    seeds = load_seeds()
    assert len(seeds) == 50
    assert len({s.id for s in seeds}) == 50
    assert len({s.name.lower() for s in seeds}) == 50


# ---------------------------------------------------------------- the brief's example
@pytest.mark.parametrize("raw", ["OpenAI", "OpenAI, Inc.", "Open AI", "openai", "OPENAI INC", "Open-AI LLC", "OpenAI OpCo, LLC"])
def test_openai_variants(r, raw):
    res = r.match_seed(raw)
    assert res is not None and res.canonical == "OpenAI", raw


# ---------------------------------------------------------------- every method
@pytest.mark.parametrize("raw,domain,canonical,method", [
    ("Anthropic", None, "Anthropic", "exact"),
    ("databricks", None, "Databricks", "exact"),
    ("Anthropic, PBC", None, "Anthropic", "legal"),
    ("Mistral AI SAS", None, "Mistral AI", "legal"),
    ("Hugging Face, Inc.", None, "Hugging Face", "legal"),
    ("Eleven Labs Inc.", None, "ElevenLabs", "legal"),
    ("Weights and Biases", None, "Weights & Biases", "exact"),
    ("Hugging-Face", None, "Hugging Face", "compact"),
    ("Mercorcareers", None, "Mercor", "noise"),
    ("Cohere Technologies Inc", None, "Cohere", "legal"),        # alias after legal-suffix strip
    ("Perplexity Technologies", None, "Perplexity", "descriptor"),
    ("Anthropc", None, "Anthropic", "fuzzy"),                     # typo
    ("Some Brand Name", "https://www.anthropic.com/careers", "Anthropic", "domain"),
    ("Cursor", None, "Anysphere", "exact"),                       # product name -> company
    ("Z.ai", None, "Zhipu AI", "exact"),
])
def test_methods(r, raw, domain, canonical, method):
    res = r.match_seed(raw, domain=domain)
    assert res is not None, raw
    assert (res.canonical, res.method) == (canonical, method)


# ---------------------------------------------------------------- must NOT match
@pytest.mark.parametrize("raw", [
    "Openreach", "OpenAI Capital Partners", "Anthropic Ventures Fund", "Scale Computing",
    "Lambda School", "Figure Eight Federal", "Harvey Nichols", "Writer's Digest",
    "Meta", "Google", "Mistral Wind Energy GmbH", "Groq Foods", "Coherent Corp",
])
def test_no_false_positives(r, raw):
    assert r.match_seed(raw) is None, f"{raw} -> {r.match_seed(raw)}"


def test_strip_legal_cases():
    assert strip_legal("TISTA Science and Technology Corporation") == "TISTA Science and Technology"
    assert strip_legal("Ample Insight Inc.") == "Ample Insight"
    assert strip_legal("Acme GmbH & Co. KG") == "Acme"
    assert strip_legal("Inc") == "Inc"                       # never empties a name
    assert keys("Acme (YC W24)").core == "Acme"


# ---------------------------------------------------------------- new entities (dynamic canonicalisation)
def test_new_entities_cluster_and_pick_display(r):
    names = ["Acme Robotics, Inc.", "ACME ROBOTICS", "Acme Robotics", "Acme Robotics",
             "Centene Corporation", "Artefactlinkedin", "Artefact", "Artefact"]
    res = r.resolve_batch(names)
    assert {res[n].canonical for n in names[:4]} == {"Acme Robotics"}
    assert res["Acme Robotics, Inc."].is_new and res["Acme Robotics, Inc."].canonical_id == "new:acmerobotics"
    assert res["Centene Corporation"].canonical == "Centene"
    assert res["Artefactlinkedin"].canonical == "Artefact"


def test_new_entity_typo_merge(r):
    res = r.resolve_batch(["Quantumleap Analytics"] * 3 + ["Quantumleap Analytcs"])
    assert res["Quantumleap Analytcs"].canonical == "Quantumleap Analytics"
    assert res["Quantumleap Analytcs"].method == "new-merge-fuzzy"


def test_batch_is_order_independent(r):
    names = ["OpenAI, Inc.", "Open AI", "Acme Robotics", "ACME ROBOTICS", "acme robotics inc", "Anthropc",
             "Quantumleap Analytics", "Quantumleap Analytcs", "Mercorcareers", "Beta Labs", "Beta Labs Ltd"]
    base = r.resolve_batch(names)
    for seed in range(5):
        shuffled = names[:]
        random.Random(seed).shuffle(shuffled)
        assert r.resolve_batch(shuffled) == base


# ---------------------------------------------------------------- news mentions
def test_mentions_case_and_strictness():
    m = MentionMatcher()
    text = ("OpenAI and Anthropic raised money; Google DeepMind replied. Harvey AI expands, "
            "while Harvey (the CEO's dog) naps. Financial stability matters. Scale AI hires. "
            "Writers love Cursor. Mistral released a model.")
    found = [x.canonical for x in m.find(text)]
    assert found == ["OpenAI", "Anthropic", "Google DeepMind", "Harvey", "Scale AI", "Anysphere", "Mistral AI"]
    assert "Stability AI" not in found          # lowercase "stability"
    assert "Writer" not in found                # bare common word
    deepmind = [x for x in m.find("DeepMind and Google DeepMind") if x.canonical == "Google DeepMind"][0]
    assert deepmind.count == 2


# ---------------------------------------------------------------- pass over stored records
async def test_resolution_pass_and_mapping_log(tmp_path):
    store = RecordStore(tmp_path / "db.sqlite3")
    now = datetime.now(timezone.utc)

    def job(i, company):
        return (f"job:{i}", JobRecord(source=Source(name="B", url=f"https://b.example/{i}"),
                                      content=JobContent(title="ML Engineer", company=company, rawCompany=company, date=now)))
    await store.upsert([job(1, "OpenAI, Inc."), job(2, "Open AI"), job(3, "Acme Robotics Inc"), job(4, "Acme Robotics")])
    await store.upsert([("news:1", NewsRecord(source=Source(name="N", url="https://n.example/1"),
                                             content=NewsContent(title="Anthropic and OpenAI", published_date=now,
                                                                 full_text="Hugging Face too.")))])
    summary = await run_resolution(store, tmp_path)
    jobs = {k: b["content"] for k, b in store.iter_items("JOB")}
    assert jobs["job:1"]["company"] == "OpenAI" and jobs["job:1"]["rawCompany"] == "OpenAI, Inc."
    assert jobs["job:3"]["company"] == "Acme Robotics"
    news = dict(store.iter_items("NEWS"))["news:1"]["content"]
    assert news["companies"] == ["Anthropic", "OpenAI", "Hugging Face"]

    rows = list(csv.DictReader(open(tmp_path / "entity_mapping_log.csv", encoding="utf-8")))
    by_raw = {(x["raw_name"], x["record_type"]): x for x in rows}
    assert by_raw[("OpenAI, Inc.", "JOB")]["canonical_name"] == "OpenAI"
    assert by_raw[("OpenAI, Inc.", "JOB")]["method"] == "legal"
    assert by_raw[("Open AI", "JOB")]["method"] == "exact"
    assert by_raw[("Anthropic", "NEWS")]["method"] == "mention"
    assert rows[0]["is_seed_entity"] == "True"       # interesting rows first
    assert summary["matched_seed"] == 2

    # idempotent: a second pass resolves raw fields again and changes nothing
    await run_resolution(store, tmp_path)
    assert {k: b["content"]["company"] for k, b in store.iter_items("JOB")} == {k: v["company"] for k, v in jobs.items()}
    store.close()


def test_distinct_websites_or_digits_block_typo_merge(r):
    res = r.resolve_batch([("Caire Health", "caire.health"), ("Cair Health", "cair.health")])
    assert res["Caire Health"].canonical == "Caire Health" and res["Cair Health"].canonical == "Cair Health"
    assert res["Caire Health"].canonical_id != res["Cair Health"].canonical_id
    res = r.resolve_batch(["Fountain9", "Fountain9", "Fountain"])
    assert res["Fountain"].canonical == "Fountain" and res["Fountain9"].canonical == "Fountain9"
    # one side without a website is still treated as a typo of the other
    res = r.resolve_batch([("Quantumleap Analytics", "qla.ai"), ("Quantumleap Analytcs", None)])
    assert res["Quantumleap Analytcs"].canonical == "Quantumleap Analytics"


def test_own_website_blocks_approximate_seed_match(r):
    assert r.match_seed("Cognitio Labs", domain="https://cognitiolabs.com") is None
    assert r.match_seed("Perplexity Technologies", domain="https://perplexity-tech.example") is None
    assert r.match_seed("Anthropic PBC", domain="https://claude.ai").canonical == "Anthropic"   # exact forms still win


async def test_startup_website_is_identity(tmp_path):
    from frontier_atlas.schemas import StartupContent, StartupData, StartupRecord
    store = RecordStore(tmp_path / "db.sqlite3")

    def st(slug, name, web):
        return (f"startup:yc:{slug}", StartupRecord(
            source=Source(name="YC", url=f"https://www.ycombinator.com/companies/{slug}"),
            content=StartupContent(entityName=name, rawName=name, data=StartupData(website=web))))
    await store.upsert([
        st("cohere", "Cohere", "https://cohere.io"),                 # namesake of the seed Cohere (cohere.com)
        st("scale-ai", "Scale AI", "http://scale.com"),              # the real seed entity
        st("atlas-a", "Atlas", "https://atlas-a.example"),           # two different startups named Atlas
        st("atlas-b", "Atlas", "https://atlas-b.example"),
    ])
    await store.upsert([job(1, "Cohere")])
    await run_resolution(store, tmp_path)
    rows = list(csv.DictReader(open(tmp_path / "entity_mapping_log.csv", encoding="utf-8")))
    by = {(x["raw_name"], x["record_type"], x["canonical_id"]): x for x in rows}
    assert ("Cohere", "STARTUP", "new:cohere@cohere.io") in by
    assert by[("Cohere", "STARTUP", "new:cohere@cohere.io")]["method"] == "new-site-conflict"
    assert ("Cohere", "JOB", "cohere") in by                       # no site evidence: stays the seed
    assert by[("Scale AI", "STARTUP", "scale-ai")]["method"] == "domain"
    assert {x["canonical_id"] for x in rows if x["raw_name"] == "Atlas"} == {"new:atlas@atlas-a.example", "new:atlas@atlas-b.example"}
    store.close()


def job(i, company):
    now = datetime.now(timezone.utc)
    return (f"job:{i}", JobRecord(source=Source(name="B", url=f"https://b.example/{i}"),
                                  content=JobContent(title="ML Engineer", company=company, rawCompany=company, date=now)))
