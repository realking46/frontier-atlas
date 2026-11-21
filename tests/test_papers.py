from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

from frontier_atlas.core.http import FetchError, HttpFetcher
from frontier_atlas.crawlers.bulk.github_links import choose_repo, find_repos
from frontier_atlas.crawlers.bulk.github_metrics import GitHubMetrics, RepoMetrics
from frontier_atlas.crawlers.bulk.paper_types import PaperCandidate, arxiv_id_from
from frontier_atlas.crawlers.bulk.papers_arxiv import parse_feed
from frontier_atlas.crawlers.bulk.papers_hf import _to_candidate
from frontier_atlas.crawlers.bulk.papers_pwc import parse_paper_page
from frontier_atlas.pipeline.papers import PaperMerger, to_record

from .conftest import make_settings


# ---------------------------------------------------------------- links
def test_find_repos_normalises_and_filters():
    text = ("Code: https://github.com/Foo-Lab/BarNet.git, see also github.com/foo-lab/barnet "
            "and https://github.com/features/copilot and (https://github.com/x/y).")
    assert find_repos(text) == ["foo-lab/barnet", "x/y"]


def test_choose_repo_prefers_title_match_over_frameworks():
    cands = ["huggingface/transformers", "vllm-project/vllm", "zai-org/glm-5", "random/tools"]
    assert choose_repo(cands, "GLM-5.2: Built for Long-Horizon Tasks") == "zai-org/glm-5"
    assert choose_repo(["vllm-project/vllm"], "Some Paper") is None
    assert choose_repo(["a/first", "b/second"], "Unrelated Title") == "a/first"


def test_arxiv_id_from():
    assert arxiv_id_from("http://arxiv.org/abs/2611.26624v1") == "2611.26624"
    assert arxiv_id_from("no id") is None


# ---------------------------------------------------------------- arXiv
ARXIV_XML = """<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/" xmlns:arxiv="http://arxiv.org/schemas/atom" xmlns="http://www.w3.org/2005/Atom">
  <opensearch:totalResults>2</opensearch:totalResults>
  <entry>
    <id>http://arxiv.org/abs/2611.26624v1</id>
    <published>2026-11-19T18:59:34Z</published>
    <title>PlayCraft: Designing Engaging
      Games</title>
    <summary>We propose PlayCraft. Code at https://github.com/play/playcraft.</summary>
    <author><name>Ada Lovelace</name></author><author><name>Alan Turing</name></author>
    <arxiv:comment>10 pages</arxiv:comment>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2611.11111v2</id>
    <published>2026-11-18T10:00:00Z</published>
    <title>No Code Paper</title>
    <summary>Nothing here.</summary>
    <author><name>Grace Hopper</name></author>
  </entry>
</feed>"""


def test_parse_arxiv_feed():
    total, papers = parse_feed(ARXIV_XML)
    assert total == 2 and len(papers) == 2
    p = papers[0]
    assert p.arxiv_id == "2611.26624"
    assert p.title == "PlayCraft: Designing Engaging Games"
    assert p.authors == ["Ada Lovelace", "Alan Turing"]
    assert p.github_url == "https://github.com/play/playcraft"
    assert p.published == datetime(2026, 11, 19, 18, 59, 34, tzinfo=timezone.utc)
    assert papers[1].github_url is None


# ---------------------------------------------------------------- HF
def test_hf_candidate():
    paper = {"id": "2611.17621", "title": "AscendGYM", "publishedAt": "2026-11-17T00:00:00.000Z",
             "authors": [{"name": "A"}, {"name": "Hidden", "hidden": True}],
             "githubRepo": "https://github.com/SLAI-AITP/AscendGYM", "githubStars": 33}
    c = _to_candidate(paper, "https://huggingface.co/papers/2611.17621")
    assert c.github_url == "https://github.com/slai-aitp/ascendgym"
    assert c.github_stars == 33
    assert c.authors == ["A"]
    assert c.paper_url == "https://arxiv.org/abs/2611.17621"


# ---------------------------------------------------------------- PwC
PWC_HTML = """<html><head>
<script type="application/ld+json">{"@context":"https://schema.org","@graph":[{"@type":"ScholarlyArticle",
"headline":"GLM-5.2: Built for Long-Horizon Tasks","author":[{"@type":"Person","name":"Z. Team"}],
"abstract":"An open-weight model."}]}</script></head>
<body><a href="https://github.com/huggingface/transformers">hf</a>
<a href="https://github.com/zai-org/GLM-5">code</a>
<a href="https://arxiv.org/abs/2611.00001">arXiv</a></body></html>"""


def test_parse_pwc_page():
    c = parse_paper_page(PWC_HTML, "https://paperswithcode.co/paper/98456")
    assert c.title.startswith("GLM-5.2")
    assert c.authors == ["Z. Team"]
    assert c.github_url == "https://github.com/zai-org/glm-5"
    assert c.arxiv_id == "2611.00001"


# ---------------------------------------------------------------- merge + record
def _cand(src, **kw):
    base = dict(source_name=src, source_url=f"https://{src}.example/x", title="T", authors=["A"],
                arxiv_id="2611.00001")
    base.update(kw)
    return PaperCandidate(**base)


def test_merger_priority_and_dedup():
    m = PaperMerger()
    m.add(_cand("paperswithcode", github_url="https://github.com/a/pwc"))
    m.add(_cand("arxiv", title="Authoritative Title", authors=["X", "Y"]))
    m.add(_cand("huggingface", github_url="https://github.com/a/hf", github_stars=5))
    assert len(m.by_key) == 1
    c = next(iter(m.by_key.values()))
    assert c.title == "Authoritative Title" and c.authors == ["X", "Y"]
    assert c.github_url == "https://github.com/a/hf" and c.source_name == "huggingface"
    assert m.sources[c.key] == {"paperswithcode", "arxiv", "huggingface"}


def test_to_record_prefers_live_stars():
    c = _cand("huggingface", github_url="https://github.com/old/name", github_stars=5)
    now = datetime.now(timezone.utc)
    rec = to_record(c, {"huggingface"}, {"old/name": RepoMetrics("new/name", 120, checked_at=now)})
    assert rec.content.github_stars == 120
    assert rec.content.github_stars_source == "github_api"
    assert rec.content.github_url == "https://github.com/new/name"   # follows rename
    rec2 = to_record(c, {"huggingface"}, {})
    assert rec2.content.github_stars == 5 and rec2.content.github_stars_source == "huggingface"
    rec3 = to_record(c, {"huggingface"}, {"old/name": RepoMetrics("old/name", None, exists=False)})
    assert rec3.content.github_stars is None and rec3.content.github_repo_exists is False
    assert rec.to_row()["content.authors"] == "A"


# ---------------------------------------------------------------- network layer
@pytest.fixture
def no_sleep(monkeypatch):
    async def instant(_s):
        return None
    monkeypatch.setattr("frontier_atlas.core.http.asyncio.sleep", instant)


async def test_fetcher_retries_429_then_succeeds(no_sleep):
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, headers={"retry-after": "0"})
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with HttpFetcher(make_settings(), client=client) as http:
        assert await http.get_json("https://api.example/x") == {"ok": True}
        assert http.stats["retry_429"] == 2


async def test_fetcher_does_not_retry_404(no_sleep):
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    async with HttpFetcher(make_settings(), client=client) as http:
        with pytest.raises(FetchError) as ei:
            await http.get("https://api.example/missing")
        assert ei.value.status == 404 and http.stats["requests"] == 1


async def test_github_graphql_batching(no_sleep):
    seen = []

    def handler(req: httpx.Request):
        q = json.loads(req.content)["query"]
        seen.append(q)
        n = q.count("repository(")
        data = {f"r{i}": {"nameWithOwner": f"Org/Repo{i}", "stargazerCount": i * 10, "forkCount": 1,
                          "isArchived": False, "pushedAt": "2026-11-01T00:00:00Z"} for i in range(n)}
        data["r1"] = None  # one deleted repo
        return httpx.Response(200, json={"data": data})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with HttpFetcher(make_settings(), client=client) as http:
        gh = GitHubMetrics(http, token="t", batch_size=3)
        res = await gh.fetch([f"org/repo{i}" for i in range(5)])
    assert len(seen) == 2                       # 5 repos / batch of 3 -> 2 requests
    assert res["org/repo0"].stars == 0 and res["org/repo2"].stars == 20
    assert res["org/repo1"].exists is False
