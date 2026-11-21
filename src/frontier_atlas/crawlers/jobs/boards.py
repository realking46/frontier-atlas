"""Job board adapters. Each yields pages of `JobPosting`, newest first.

| board            | access               | date field           | remote          |
|------------------|----------------------|----------------------|-----------------|
| Himalayas        | JSON API, cursor     | pubDate (epoch)      | remote-only     |
| Jobicy           | JSON API             | pubDate (ISO)        | remote-only     |
| We Work Remotely | RSS                  | pubDate (RFC 2822)   | remote-only     |
| Arbeitnow        | JSON API, pages      | created_at (epoch)   | `remote` flag   |
| HN Jobs (YC)     | HTML listing + page  | span.age (ISO/relative) | from page    |

RemoteOK was evaluated and dropped: its public API is delayed, so nothing it
returns can satisfy the 24-hour freshness guarantee.
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urljoin

from selectolax.lexbor import LexborHTMLParser

from ...core.http import HttpFetcher
from ...freshness.date_parser import DateResult, parse_date
from ..text import strip_html

UTC = timezone.utc


@dataclass
class JobPosting:
    source: str
    url: str
    title: str | None
    company: str | None
    date: DateResult | None
    location: str | None = None
    remote: bool | None = None
    tags: list[str] = field(default_factory=list)
    description: str = ""                 # plain text
    extraction: str = "structured"


def _dr(raw, source: str, conf: float, now: datetime) -> DateResult | None:
    if raw in (None, ""):
        return None
    p = parse_date(str(raw), now)
    return DateResult(p.dt, source, conf, str(raw)[:80], p.date_only, p.relative, p.granularity_s) if p else None


class JobBoard:
    name: str = ""
    label: str = ""
    needs_detail: bool = False   # crawler must fetch the job page for title/company/description
    min_interval_s: float = 0.0

    async def pages(self, http: HttpFetcher, now: datetime) -> AsyncIterator[list[JobPosting]]:
        raise NotImplementedError
        yield []  # pragma: no cover

    async def enrich(self, http: HttpFetcher, post: JobPosting, html: str) -> None:
        """Fill fields from the job's own page (only for needs_detail boards)."""


# ---------------------------------------------------------------------------- Himalayas
class Himalayas(JobBoard):
    name, label = "himalayas", "Himalayas"
    API = "https://himalayas.app/jobs/api"
    min_interval_s = 0.3

    # ~2,000+ jobs/day at 20 per page; paging stops at the 24 h boundary, the cap is a safety net.
    # Hourly incremental runs stop after 1-3 pages (first all-duplicate page).
    def __init__(self, max_pages: int = 300):
        self.max_pages = max_pages

    async def pages(self, http, now):
        cursor = None
        for _ in range(self.max_pages):
            params = {"limit": 20}
            if cursor:
                params["cursor"] = cursor
            data = await http.get_json(self.API, params=params)
            jobs = data.get("jobs") or []
            yield [self._post(j, now) for j in jobs if j.get("guid") or j.get("applicationLink")]
            cursor = data.get("nextCursor")
            if not cursor or not jobs:
                return

    def _post(self, j: dict, now) -> JobPosting:
        locs = j.get("locationRestrictions") or []
        return JobPosting(
            source=self.name,
            url=j.get("guid") or j.get("applicationLink"),
            title=j.get("title"),
            company=j.get("companyName"),
            date=_dr(j.get("pubDate"), "api:pubDate", 0.95, now),
            location=", ".join(locs) if locs else "Worldwide",
            remote=True,
            tags=(j.get("categories") or []) + (j.get("parentCategories") or []),
            description=strip_html(j.get("description") or j.get("excerpt")),
        )


# ---------------------------------------------------------------------------- Jobicy
class Jobicy(JobBoard):
    name, label = "jobicy", "Jobicy"
    API = "https://jobicy.com/api/v2/remote-jobs"
    QUERIES = ({"count": 100}, {"count": 100, "tag": "ai"}, {"count": 100, "tag": "machine learning"},
               {"count": 100, "industry": "data-science"})
    min_interval_s = 1.0

    async def pages(self, http, now):
        for q in self.QUERIES:
            data = await http.get_json(self.API, params=q)
            yield [self._post(j, now) for j in data.get("jobs") or [] if j.get("url")]

    def _post(self, j: dict, now) -> JobPosting:
        industry = j.get("jobIndustry") or []
        jtype = j.get("jobType") or []
        return JobPosting(
            source=self.name,
            url=j["url"],
            title=strip_html(j.get("jobTitle")),
            company=strip_html(j.get("companyName")),
            date=_dr(j.get("pubDate"), "api:pubDate", 0.95, now),
            location=j.get("jobGeo"),
            remote=True,
            tags=[strip_html(x) for x in (industry if isinstance(industry, list) else [industry])]
                 + (jtype if isinstance(jtype, list) else [jtype]),
            description=strip_html(j.get("jobDescription") or j.get("jobExcerpt")),
        )


# ---------------------------------------------------------------------------- We Work Remotely
class WeWorkRemotely(JobBoard):
    name, label = "weworkremotely", "We Work Remotely"
    FEED = "https://weworkremotely.com/remote-jobs.rss"
    min_interval_s = 1.0

    async def pages(self, http, now):
        xml = await http.get_text(self.FEED)
        root = ET.fromstring(xml.lstrip("﻿").strip())
        out = []
        for item in root.iter("item"):
            g = {(c.tag.rsplit("}", 1)[-1]): (c.text or "") for c in item}
            raw_title = " ".join(g.get("title", "").split())
            company, _, title = raw_title.partition(": ")
            if not title:
                company, title = None, raw_title
            out.append(JobPosting(
                source=self.name,
                url=g.get("link") or g.get("guid"),
                title=title or None,
                company=company or None,
                date=_dr(g.get("pubDate"), "rss:pubDate", 0.9, now),
                location=g.get("region") or None,
                remote=True,
                tags=[g["category"]] if g.get("category") else [],
                description=strip_html(g.get("description")),
            ))
        yield out


# ---------------------------------------------------------------------------- Arbeitnow
class Arbeitnow(JobBoard):
    name, label = "arbeitnow", "Arbeitnow"
    API = "https://www.arbeitnow.com/api/job-board-api"
    min_interval_s = 1.0

    def __init__(self, max_pages: int = 10):
        self.max_pages = max_pages

    async def pages(self, http, now):
        for page in range(1, self.max_pages + 1):
            data = await http.get_json(self.API, params={"page": page})
            jobs = data.get("data") or []
            yield [self._post(j, now) for j in jobs if j.get("url")]
            if not (data.get("links") or {}).get("next") or not jobs:
                return

    def _post(self, j: dict, now) -> JobPosting:
        return JobPosting(
            source=self.name,
            url=j["url"],
            title=j.get("title"),
            company=j.get("company_name"),
            date=_dr(j.get("created_at"), "api:created_at", 0.95, now),
            location=j.get("location"),
            remote=j.get("remote"),
            tags=(j.get("tags") or []) + (j.get("job_types") or []),
            description=strip_html(j.get("description")),
        )


# ---------------------------------------------------------------------------- Hacker News jobs (YC)
HN_TITLE_RE = re.compile(
    r"^(?P<company>.+?)\s*\((?P<batch>YC [A-Z]\d{2,4})\)\s*(?:is\s+)?hiring\b[\s:,\-–—]*(?:an?\s+)?(?P<role>.*)$",
    re.I,
)


class HackerNewsJobs(JobBoard):
    """news.ycombinator.com/jobs – YC startups. The listing shows relative ages
    ("3 hours ago") with the exact timestamp in the <span class="age" title> attribute.
    Titles are free-form ("Evidence (YC S21) Is Hiring"), so title/company come from
    the linked job page (JSON-LD JobPosting) or, failing that, the LLM / title rules."""

    name, label = "hn-jobs", "Hacker News Jobs (YC)"
    LISTING = "https://news.ycombinator.com/jobs"
    needs_detail = True
    min_interval_s = 2.0

    async def pages(self, http, now):
        html = await http.get_text(self.LISTING)
        yield parse_hn_listing(html, now)

    async def enrich(self, http, post: JobPosting, html: str) -> None:
        jp = find_jobposting_jsonld(html)
        if jp:
            post.title = strip_html(jp.get("title")) or post.title
            org = jp.get("hiringOrganization")
            if isinstance(org, dict) and org.get("name"):
                post.company = org["name"]
            loc_type = str(jp.get("jobLocationType") or "")
            locs = jp.get("jobLocation") or []
            locs = locs if isinstance(locs, list) else [locs]
            names = []
            for loc in locs:
                addr = (loc or {}).get("address") or {}
                if isinstance(addr, dict):
                    names.append(", ".join(x for x in (addr.get("addressLocality"), addr.get("addressCountry"))
                                           if isinstance(x, str) and x))
            post.location = "; ".join(n for n in names if n) or post.location
            if "TELECOMMUTE" in loc_type.upper():
                post.remote = True
            post.description = strip_html(jp.get("description")) or post.description
            post.extraction = "structured"
        if not post.description:
            from ..text import extract_article
            post.description = extract_article(html).text


def parse_hn_listing(html: str, now: datetime) -> list[JobPosting]:
    tree = LexborHTMLParser(html)
    out = []
    for row in tree.css("tr.athing"):
        a = row.css_first("span.titleline > a")
        if a is None:
            continue
        headline = a.text(strip=True)
        href = urljoin("https://news.ycombinator.com/", a.attributes.get("href") or "")
        sub = row.next
        while sub is not None and sub.tag != "tr":
            sub = sub.next
        age = sub.css_first("span.age") if sub is not None else None
        date = None
        if age is not None:
            exact = (age.attributes.get("title") or "").split(" ")[0]
            date = _dr(exact, "listing:age-title", 0.9, now) if exact else None
            if date is None:   # fall back to the visible relative text, e.g. "3 hours ago"
                date = _dr(age.text(strip=True), "listing:relative", 0.75, now)
        m = HN_TITLE_RE.match(headline)
        out.append(JobPosting(
            source="hn-jobs",
            url=href,
            title=(m.group("role").strip() or None) if m else None,
            company=m.group("company").strip() if m else None,
            date=date,
            tags=[m.group("batch")] if m else [],
            description=headline,
            extraction="rules",
        ))
    return out


def find_jobposting_jsonld(html: str) -> dict | None:
    tree = LexborHTMLParser(html)
    for node in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.text() or "")
        except ValueError:
            continue
        stack = [data]
        while stack:
            x = stack.pop()
            if isinstance(x, list):
                stack.extend(x)
            elif isinstance(x, dict):
                t = x.get("@type")
                if t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t):
                    return x
                stack.extend(v for k, v in x.items() if k == "@graph")
    return None


ALL_BOARDS = (Himalayas, Jobicy, WeWorkRemotely, Arbeitnow, HackerNewsJobs)
