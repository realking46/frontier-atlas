from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from frontier_atlas.freshness import FreshnessGate, ListingItem, SeenStore, Verdict, canonical_url, simhash
from frontier_atlas.freshness.heuristics import bracket_by_listing, frontier_new_keys
from frontier_atlas.freshness.date_parser import DateResult
from frontier_atlas.freshness.seen_store import hamming

UTC = timezone.utc
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
DAY = timedelta(hours=24)


# ---------------------------------------------------------------- canonical URLs
def test_canonical_url_strips_noise():
    a = "https://www.TechNews.com/2026/11/20/story/?utm_source=x&b=2&a=1&fbclid=zz#comments"
    b = "http://technews.com/2026/11/20/story?a=1&b=2"
    c = "https://technews.com/2026/11/20/story/amp/"
    d = "https://amp.technews.com/2026/11/20/story?outputType=amp"
    assert canonical_url(a) == canonical_url(b) == "https://technews.com/2026/11/20/story?a=1&b=2"
    assert canonical_url(c) == canonical_url(d) == "https://technews.com/2026/11/20/story"


# ---------------------------------------------------------------- claims
def test_claim_is_exclusive_and_lease_expires():
    s = SeenStore(":memory:", lease_seconds=60)
    assert s.claim("k", source="x", url="u", node="A", now=1000)
    assert not s.claim("k", source="x", url="u", node="B", now=1010)      # A holds the lease
    assert s.claim("k", source="x", url="u", node="B", now=1100)          # A died, lease expired
    s.mark_done("k")
    assert not s.claim("k", source="x", url="u", node="C", now=99999)     # done is final


def test_release_allows_retry():
    s = SeenStore(":memory:")
    assert s.claim("k", source="x", url="u")
    s.release("k")
    assert s.claim("k", source="x", url="u")


# ---------------------------------------------------------------- simhash
def _article(seed: int, n: int = 300) -> list[str]:
    rnd = random.Random(seed)
    vocab = [f"w{i}" for i in range(2000)]
    return [rnd.choice(vocab) for _ in range(n)]


def test_simhash_near_and_far():
    a = _article(1)
    b = list(a)
    b[150] = "changed"
    assert hamming(simhash(" ".join(a)), simhash(" ".join(b))) <= 3
    assert hamming(simhash(" ".join(a)), simhash(" ".join(_article(2)))) > 15


def test_near_duplicate_lookup():
    s = SeenStore(":memory:")
    text = " ".join(_article(7))
    s.claim("k1", source="x", url="u1")
    s.mark_done("k1", simhash_value=simhash(text))
    edited = text.replace("w5 ", "w6 ", 1)
    assert s.find_near_duplicate(simhash(edited)) == "k1"
    assert s.find_near_duplicate(simhash(" ".join(_article(8)))) is None


# ---------------------------------------------------------------- heuristics
def _d(hours_ago: float) -> DateResult:
    return DateResult(NOW - timedelta(hours=hours_ago), "t", 0.9, "")


def test_bracketing_reverse_chronological():
    dates = [_d(1), None, _d(3), _d(5), None, _d(30), None]
    v = bracket_by_listing(dates, NOW, DAY)
    assert v[1] is Verdict.FRESH          # above a 3h-old item
    assert v[4] is None                   # between 5h and 30h -> cannot be proven either way
    assert v[6] is Verdict.STALE          # below a 30h-old item


def test_bracketing_ignores_unordered_lists():
    dates = [_d(30), None, _d(1), _d(50), _d(2)]
    assert bracket_by_listing(dates, NOW, DAY) == [None] * 5


def test_frontier():
    assert frontier_new_keys(["n1", "n2", "old1", "n3", "old2"], {"n1", "n2", "n3"}) == {"n1", "n2"}


# ---------------------------------------------------------------- gate end-to-end
def test_gate_cold_start_then_new_since_last_run():
    store = SeenStore(":memory:")
    gate = FreshnessGate(store, window=DAY)

    # run 1: undated listing, no history -> nothing can be proven
    run1 = gate.begin("blog", now=NOW)
    decs = gate.screen_listing(run1, [ListingItem(f"https://blog.example/p{i}") for i in range(5)], now=NOW)
    assert all(d.needs_page for d in decs)
    for d in decs:
        gate.screen_page(d, "<html><body>no date here</body></html>", now=NOW)
        assert d.verdict is Verdict.UNCERTAIN
        assert gate.finalize(d, "text") is False
    gate.end(run1)

    # run 2, one hour later: two new posts appear on top
    later = NOW + timedelta(hours=1)
    run2 = gate.begin("blog", now=later)
    urls = ["https://blog.example/new1", "https://blog.example/new2"] + [f"https://blog.example/p{i}" for i in range(5)]
    decs = gate.screen_listing(run2, [ListingItem(u) for u in urls], now=later)
    assert [d.verdict for d in decs[2:]] == [Verdict.DUPLICATE] * 5
    for d in decs[:2]:
        gate.screen_page(d, "<html><body>still no date</body></html>", now=later)
        assert d.verdict is Verdict.FRESH and "heuristic" in d.reason
        assert d.date.source == "heuristic:first_seen"
        assert gate.finalize(d, f"unique article text {d.url} " * 10)
    gate.end(run2)


def test_gate_listing_dates_and_page_dates():
    gate = FreshnessGate(SeenStore(":memory:"), window=DAY)
    run = gate.begin("jobs", now=NOW)
    items = [
        ListingItem("https://jobs.example/a", "2 hours ago"),
        ListingItem("https://jobs.example/b", "3 days ago"),
        ListingItem("https://jobs.example/c"),            # undated -> page decides
    ]
    a, b, c = gate.screen_listing(run, items, now=NOW)
    assert a.verdict is Verdict.FRESH and not a.needs_page
    assert b.verdict is Verdict.STALE and not b.claimed
    assert c.needs_page
    html = '<html><head><script type="application/ld+json">{"@type":"JobPosting","datePosted":"2026-11-20T09:00:00Z"}</script></head></html>'
    gate.screen_page(c, html, now=NOW)
    assert c.verdict is Verdict.FRESH and c.date.source == "jsonld:datePosted"


def test_gate_two_nodes_never_double_process():
    store = SeenStore(":memory:")
    g1, g2 = FreshnessGate(store, node_id="n1"), FreshnessGate(store, node_id="n2")
    items = [ListingItem("https://n.example/x?utm_source=a", "1 hour ago")]
    d1 = g1.screen_listing(g1.begin("s", now=NOW), items, now=NOW)[0]
    d2 = g2.screen_listing(g2.begin("s", now=NOW), [ListingItem("https://www.n.example/x/", "1 hour ago")], now=NOW)[0]
    assert d1.claimed and d1.verdict is Verdict.FRESH
    assert d2.verdict is Verdict.DUPLICATE and not d2.claimed


def test_templated_descriptions_with_different_titles_are_not_duplicates():
    store = SeenStore(":memory:")
    text = " ".join(_article(5))
    store.claim("k1", source="x", url="u1")
    store.mark_done("k1", simhash_value=simhash(text), label="CDI - Context Analytics Engineer Manager")
    assert store.find_near_duplicate(simhash(text), label="CDI - Senior Context Analytics Engineer") is None
    assert store.find_near_duplicate(simhash(text), label="CDI - Context Analytics Engineer Manager") == "k1"
    assert store.find_near_duplicate(simhash(text)) == "k1"          # no label (news): text alone decides


def test_gate_drops_syndicated_copy():
    gate = FreshnessGate(SeenStore(":memory:"))
    run = gate.begin("s", now=NOW)
    text = " ".join(_article(42))
    d1, d2 = gate.screen_listing(run, [ListingItem("https://a.example/x", "1 hour ago"),
                                       ListingItem("https://b.example/y", "1 hour ago")], now=NOW)
    assert gate.finalize(d1, text)
    assert not gate.finalize(d2, text + " Originally published on a.example.")
    assert d2.verdict is Verdict.DUPLICATE


async def test_in_order_early_exit_cannot_overtake():
    import asyncio
    from frontier_atlas.freshness.gate import InOrder
    order, claims = InOrder(3), []

    async def worker(i, delay, claims_text):
        try:
            await asyncio.sleep(delay)
            if not claims_text:
                return                       # exits early (e.g. stale item), never calls turn()
            await order.turn(i)
            claims.append(i)
        finally:
            await order.done(i)
    # item 0 is slow, item 1 exits early at once, item 2 is fast: 2 must still claim after 0
    await asyncio.gather(worker(0, 0.05, True), worker(1, 0, False), worker(2, 0, True))
    assert claims == [0, 2]
