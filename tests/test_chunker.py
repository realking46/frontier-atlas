from __future__ import annotations

from frontier_atlas.extraction.chunker import count_tokens, fit_to_budget, html_to_text
from frontier_atlas.extraction.grounding import check
from frontier_atlas.schemas import StartupExtraction

HTML = """
<html><head>
  <title>Acme Robotics | StartupDir</title>
  <meta name="description" content="Warehouse robots">
  <meta property="article:published_time" content="2026-11-20T08:00:00Z">
  <script type="application/ld+json">{"@type":"Organization","name":"Acme Robotics","numberOfEmployees":120,"logo":"x.png"}</script>
  <script>var tracking = 1;</script>
</head><body>
  <nav>Home | Startups | Login</nav>
  <div class="cookie-banner">Accept all cookies</div>
  <h1>Acme Robotics</h1>
  <p>Acme builds autonomous warehouse robots.</p>
  <time datetime="2026-11-20">Nov 20</time>
  <footer>© 2026 StartupDir. All rights reserved.</footer>
</body></html>
"""


def test_html_to_text_strips_junk_keeps_metadata():
    text = html_to_text(HTML)
    assert "[title] Acme Robotics | StartupDir" in text
    assert "[published_time] 2026-11-20T08:00:00Z" in text
    assert '"numberOfEmployees":120' in text
    assert "logo" not in text                 # pruned from JSON-LD
    assert "tracking" not in text
    assert "Accept all cookies" not in text
    assert "All rights reserved" not in text
    assert "autonomous warehouse robots" in text


def test_short_text_untouched():
    r = fit_to_budget("hello world", 1000)
    assert not r.truncated and r.text == "hello world"


def test_long_text_fits_budget_and_keeps_dense_block():
    filler = [f"Lorem ipsum filler sentence number {i} with no useful content at all here." for i in range(800)]
    filler.insert(500, "The company was founded in 2018 and has 340 employees; it raised a $20 million Series A.")
    text = "[title] Some Startup\n\n" + "\n\n".join(filler)
    r = fit_to_budget(text, 1500, kind="startup")
    assert r.truncated
    assert r.tokens <= 1500
    assert r.original_tokens > 1500 * 5
    assert "[title] Some Startup" in r.text           # head kept
    assert "340 employees" in r.text                  # dense block promoted from the middle


def test_byte_cap_enforced():
    text = "é" * 50_000
    r = fit_to_budget(text, 100_000, max_bytes=10_000)
    assert len(r.text.encode()) <= 10_000


def test_single_giant_block_is_cut():
    text = "x" * 200_000
    r = fit_to_budget(text, 500)
    assert count_tokens(r.text) <= 500


def test_grounding_fuzzy_and_numbers():
    src = "OpenAI, Inc. is based in San Francisco and has 3,000 employees. Visit openai.com"
    ex = StartupExtraction(entityName="OpenAI Inc", employeeCount=3000, website="https://www.openai.com",
                           location="San Francisco")
    rep = check(ex, src, kind="startup")
    assert rep.ok and rep.dropped == []

    ex2 = StartupExtraction(entityName="OpenAI", employeeCount=4500, location="London")
    rep2 = check(ex2, src, kind="startup")
    assert rep2.ok
    assert set(rep2.dropped) == {"employeeCount", "location"}
    assert ex2.employeeCount is None
