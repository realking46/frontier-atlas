from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from frontier_atlas.freshness.date_parser import extract_published, parse_date, tz_from_name
from frontier_atlas.freshness.heuristics import Verdict, classify

UTC = timezone.utc
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
DAY = timedelta(hours=24)


def p(s, **kw):
    r = parse_date(s, NOW, **kw)
    return r.dt if r else None


@pytest.mark.parametrize("raw,expected", [
    ("2026-11-20T08:00:00Z", datetime(2026, 11, 20, 8, 0, tzinfo=UTC)),
    ("2026-11-20T08:00:00.000+0000", datetime(2026, 11, 20, 8, 0, tzinfo=UTC)),
    ("2026-11-20T13:30:00+05:30", datetime(2026, 11, 20, 8, 0, tzinfo=UTC)),
    ("Thu, 20 Nov 2026 07:00:00 GMT", datetime(2026, 11, 20, 7, 0, tzinfo=UTC)),
    ("Fri, 20 Nov 2026 02:00:00 -0500", datetime(2026, 11, 20, 7, 0, tzinfo=UTC)),
    ("1795161600", datetime(2026, 11, 20, 8, 0, tzinfo=UTC)),
    ("1795161600000", datetime(2026, 11, 20, 8, 0, tzinfo=UTC)),
    ("2 hours ago", NOW - timedelta(hours=2)),
    ("Posted 3 days ago", NOW - timedelta(days=3)),
    ("an hour ago", NOW - timedelta(hours=1)),
    ("a day ago", NOW - timedelta(days=1)),
    ("Updated: 45 mins ago", NOW - timedelta(minutes=45)),
    ("30+ days ago", NOW - timedelta(days=30)),
    ("5m", NOW - timedelta(minutes=5)),
    ("2h", NOW - timedelta(hours=2)),
    ("just now", NOW),
    ("Yesterday at 3:15 PM", datetime(2026, 11, 19, 15, 15, tzinfo=UTC)),
    ("November 20, 2026 at 6:30 AM PST", datetime(2026, 11, 20, 14, 30, tzinfo=UTC)),
    ("20 Nov 2026 09:00", datetime(2026, 11, 20, 9, 0, tzinfo=UTC)),
    ("Nov 20, 2026 10:00 AM PT", datetime(2026, 11, 20, 18, 0, tzinfo=UTC)),     # PST in November
    ("Jul 20, 2026 10:00 AM PT", datetime(2026, 7, 20, 17, 0, tzinfo=UTC)),      # PDT in July
    ("Published on Nov 18, 2026", datetime(2026, 11, 18, tzinfo=UTC)),
])
def test_parse_formats(raw, expected):
    assert p(raw) == expected


@pytest.mark.parametrize("raw", ["Apply now", "Apply within 5 days", "Series A $20 million", "", "hello", "Top 10 tools"])
def test_non_dates_rejected(raw):
    assert parse_date(raw, NOW) is None


def test_date_only_and_granularity():
    r = parse_date("2026-11-19", NOW)
    assert r.date_only and r.granularity_s == 86400
    r = parse_date("3 hours ago", NOW)
    assert r.relative and r.granularity_s == 3600
    r = parse_date("Today", NOW)
    assert r.date_only and r.dt == datetime(2026, 11, 20, tzinfo=UTC)


def test_missing_year_rolls_back():
    jan2 = datetime(2027, 1, 2, 10, tzinfo=UTC)
    assert parse_date("Dec 31", jan2).dt.year == 2026


def test_source_timezone():
    ist = tz_from_name("Asia/Kolkata")
    assert parse_date("2026-11-20 10:00", NOW, default_tz=ist).dt == datetime(2026, 11, 20, 4, 30, tzinfo=UTC)
    # "today" is the source's calendar day: 01:00 IST on Nov 21 is still Nov 20 in UTC
    late = datetime(2026, 11, 20, 20, 0, tzinfo=UTC)
    assert parse_date("Today", late, default_tz=ist).dt == datetime(2026, 11, 20, 18, 30, tzinfo=UTC)


# ---------------------------------------------------------------- classify (24 h window)
@pytest.mark.parametrize("raw,verdict", [
    ("3 hours ago", Verdict.FRESH),
    ("23 hours ago", Verdict.FRESH),          # 23h..24h -> still inside
    ("1 day ago", Verdict.UNCERTAIN),         # 24h..48h -> cannot prove
    ("2 days ago", Verdict.STALE),
    ("Today", Verdict.FRESH),                 # whole day lies inside the last 24h at noon
    ("2026-11-19", Verdict.UNCERTAIN),        # yesterday: partly inside
    ("2026-11-18", Verdict.STALE),
    ("2026-11-20T00:30:00Z", Verdict.FRESH),
    ("2026-11-19T11:59:00Z", Verdict.STALE),
])
def test_classify(raw, verdict):
    from frontier_atlas.freshness.date_parser import DateResult
    r = parse_date(raw, NOW)
    d = DateResult(r.dt, "test", 0.9, raw, r.date_only, r.relative, r.granularity_s)
    assert classify(d, NOW, DAY) is verdict


# ---------------------------------------------------------------- page extraction
def _page(head="", body="", url=None, headers=None):
    return extract_published(f"<html><head>{head}</head><body>{body}</body></html>", url, headers=headers, now=NOW)


def test_jsonld_beats_meta_and_ignores_modified():
    head = ('<script type="application/ld+json">{"@type":"NewsArticle","datePublished":"2026-11-20T06:00:00Z",'
            '"dateModified":"2026-11-20T11:00:00Z"}</script>'
            '<meta property="article:modified_time" content="2026-11-20T11:00:00Z">')
    r = _page(head)
    assert r.source == "jsonld:datePublished" and r.dt.hour == 6


def test_jsonld_in_graph_and_jobposting():
    head = ('<script type="application/ld+json">{"@graph":[{"@type":"WebPage"},'
            '{"@type":"JobPosting","datePosted":"2026-11-20"}]}</script>')
    r = _page(head)
    assert r.source == "jsonld:datePosted" and r.date_only


def test_modified_only_page_has_no_publish_date():
    head = '<meta property="article:modified_time" content="2026-11-20T11:00:00Z">'
    assert _page(head) is None


def test_meta_time_url_byline_lastmod():
    assert _page('<meta property="article:published_time" content="2026-11-20T07:00:00Z">').source.startswith("meta:")
    assert _page(body='<time datetime="2026-11-20T09:00:00Z">9am</time>').source == "time-tag"
    r = _page(url="https://news.example/2026/11/20/some-story")
    assert r.source == "url" and r.date_only
    r = _page(body='<span class="post-date">Posted 5 hours ago</span>')
    assert r.source == "text:byline" and r.dt == NOW - timedelta(hours=5)
    r = _page(headers={"Last-Modified": "Fri, 20 Nov 2026 10:00:00 GMT"})
    assert r.source == "http:last-modified" and r.confidence < 0.5


def test_future_dates_clamped_or_dropped():
    r = _page('<meta property="article:published_time" content="2026-11-20T20:00:00Z">')  # +8h: tz bug
    assert r.dt == NOW and r.confidence < 0.9
    assert _page('<meta property="article:published_time" content="2027-03-01T00:00:00Z">') is None


def test_disagreeing_sources_lower_confidence():
    head = ('<meta property="article:published_time" content="2026-11-20T07:00:00Z">'
            '<script type="application/ld+json">{"datePublished":"2026-10-01T07:00:00Z"}</script>')
    r = _page(head)
    assert r.confidence < 0.8
