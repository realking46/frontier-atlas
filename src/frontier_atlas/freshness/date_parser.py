"""Publication-date extraction and normalisation.

Two layers:

1. `parse_date(text, now)` – turns any single date string into an aware UTC
   datetime: ISO-8601, RFC-2822 (RSS), epoch seconds/ms, relative phrases
   ("2 hours ago", "5m", "yesterday at 3:15 PM", "30+ days ago"), and free-form
   absolute dates ("Nov 20, 2026", "20 November 2026 08:00 PST").

2. `extract_published(html, url, now)` – collects date *candidates* from every
   place a page may hide one, ranks them by reliability, sanity-checks them and
   returns the best with a confidence score:

      JSON-LD datePublished / datePosted ........ 0.95
      <meta> published-time tags ................ 0.90
      <time datetime> / itemprop ................ 0.80
      date in the URL path (/2026/11/20/) ........ 0.60  (date-only)
      byline-style text ("Posted 3 hours ago") ... 0.55
      HTTP Last-Modified ......................... 0.30  (weak support only)

   Modified dates are never used as a publication date.

Every result records *where* the date came from and whether it is date-only,
because "2026-11-19" cannot prove an item is < 24 h old by itself – the
freshness gate treats that case as UNCERTAIN and falls back to heuristics.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from dateutil import parser as du_parser
from dateutil import tz as du_tz
from selectolax.lexbor import LexborHTMLParser

UTC = timezone.utc

# Abbreviations dateutil does not know by itself.
TZINFOS = {
    "UTC": 0, "GMT": 0, "Z": 0,
    "EST": -5 * 3600, "EDT": -4 * 3600, "CST": -6 * 3600, "CDT": -5 * 3600,
    "MST": -7 * 3600, "MDT": -6 * 3600, "PST": -8 * 3600, "PDT": -7 * 3600,
    "IST": 5 * 3600 + 1800, "CET": 3600, "CEST": 7200, "BST": 3600,
    "JST": 9 * 3600, "KST": 9 * 3600, "SGT": 8 * 3600, "AEST": 10 * 3600, "AEDT": 11 * 3600,
    # DST-agnostic US abbreviations ("10:00 AM PT") -> real zones, so DST is handled
    "PT": du_tz.gettz("America/Los_Angeles"), "MT": du_tz.gettz("America/Denver"),
    "CT": du_tz.gettz("America/Chicago"), "ET": du_tz.gettz("America/New_York"),
}

UNIT_SECONDS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
    "w": 604800, "wk": 604800, "wks": 604800, "week": 604800, "weeks": 604800,
    "mo": 2592000, "mos": 2592000, "month": 2592000, "months": 2592000,
    "y": 31536000, "yr": 31536000, "yrs": 31536000, "year": 31536000, "years": 31536000,
}
_UNIT_ALT = "|".join(sorted(UNIT_SECONDS, key=len, reverse=True))

RELATIVE_RE = re.compile(
    rf"\b(?P<num>\d+|an?|one)\s*\+?\s*(?P<unit>{_UNIT_ALT})\.?\s+ago\b",
    re.I,
)
# Compact listing form with no "ago": the whole string is just "5m", "2h", "3d", "1w".
COMPACT_RE = re.compile(r"^(?P<num>\d+)\s*(?P<unit>s|m|min|h|hr|d|w)$", re.I)
NOW_RE = re.compile(r"^(just now|right now|now|moments? ago|(a )?few seconds ago|seconds ago)$", re.I)
DAYWORD_RE = re.compile(r"\b(today|yesterday)\b(?:[\s,]*(?:at\s*)?(?P<time>\d{1,2}(?::\d{2})?\s*(?:am|pm)?))?", re.I)
NOISE_RE = re.compile(
    r"^\s*(posted|published|updated|last updated|date|on|reposted|active|listed|created)\s*[:\-–]?\s*",
    re.I,
)
YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
MONTH_NAME_RE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}\b|"
    r"\b\d{1,2}(st|nd|rd|th)?\s+(of\s+)?(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\b",
    re.I,
)
NUMERIC_DATE_RE = re.compile(r"\b(\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4})\b")
HAS_TIME_RE = re.compile(r"\d{1,2}:\d{2}|\d\s*(am|pm)\b", re.I)
ISO_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
URL_DATE_RE = re.compile(r"/(20\d{2})[/-](0[1-9]|1[0-2])[/-](0[1-9]|[12]\d|3[01])(?:/|-|$)")


@dataclass(slots=True)
class ParsedDate:
    dt: datetime            # aware, UTC
    date_only: bool = False # no time-of-day information
    relative: bool = False  # derived from "N units ago"
    granularity_s: int = 0  # true time lies in [dt - granularity, dt] (relative) or [dt, dt + granularity] (date-only)


@dataclass(slots=True)
class DateResult:
    dt: datetime
    source: str             # e.g. "jsonld:datePublished", "meta:article:published_time"
    confidence: float
    raw: str
    date_only: bool = False
    relative: bool = False
    granularity_s: int = 0

    @property
    def iso(self) -> str:
        return self.dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


# ====================================================================== single strings
def _to_utc(dt: datetime, default_tz) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=default_tz)
    return dt.astimezone(UTC)


def _parse_relative(s: str, now: datetime, default_tz=UTC) -> ParsedDate | None:
    if NOW_RE.match(s.strip(" .")):
        return ParsedDate(now, relative=True)
    if m := DAYWORD_RE.search(s):
        # "today"/"yesterday" refer to the *source's* calendar day
        local = now.astimezone(default_tz)
        base = local if m.group(1).lower() == "today" else local - timedelta(days=1)
        if t := m.group("time"):
            try:
                tm = du_parser.parse(t)
                dt = base.replace(hour=tm.hour, minute=tm.minute, second=0, microsecond=0)
                return ParsedDate(dt.astimezone(UTC), relative=True, granularity_s=60)
            except (ValueError, OverflowError):
                pass
        dt = base.replace(hour=0, minute=0, second=0, microsecond=0)
        return ParsedDate(dt.astimezone(UTC), date_only=True, relative=True, granularity_s=86400)
    m = RELATIVE_RE.search(s) or COMPACT_RE.match(s.strip())
    if m:
        num = m.group("num").lower()
        n = 1 if num in {"a", "an", "one"} else int(num)
        # in compact listings "m" means minutes, never months
        unit_s = UNIT_SECONDS[m.group("unit").lower()]
        # "3 hours ago" is usually floor(age): the item is 3h00m..3h59m old
        return ParsedDate(now - timedelta(seconds=n * unit_s), relative=True, granularity_s=unit_s)
    return None


def parse_date(
    value: str | int | float | None,
    now: datetime | None = None,
    *,
    default_tz=UTC,
    dayfirst: bool = False,
) -> ParsedDate | None:
    """Parse one date string. Returns None if it is not a date."""
    if value is None:
        return None
    now = now or datetime.now(UTC)

    # epoch seconds / milliseconds
    if isinstance(value, (int, float)) or (isinstance(value, str) and re.fullmatch(r"\d{10}(\d{3})?", value.strip())):
        v = float(value)
        if v > 1e12:
            v /= 1000
        try:
            return ParsedDate(datetime.fromtimestamp(v, UTC))
        except (OverflowError, OSError, ValueError):
            return None

    s = " ".join(str(value).split())
    if not s or len(s) > 200:
        return None
    s = NOISE_RE.sub("", s)

    if rel := _parse_relative(s, now, default_tz):
        return rel

    # ISO-8601 (fast path)
    iso = s.replace("Z", "+00:00").replace(" UTC", "+00:00")
    iso = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", iso)  # +0000 -> +00:00
    try:
        dt = datetime.fromisoformat(iso)
        date_only = bool(ISO_DATE_ONLY_RE.match(s))
        return ParsedDate(_to_utc(dt, default_tz), date_only=date_only, granularity_s=86400 if date_only else 0)
    except ValueError:
        pass

    # RFC 2822 (RSS pubDate)
    if re.match(r"^[A-Za-z]{3},", s):
        try:
            return ParsedDate(_to_utc(parsedate_to_datetime(s), default_tz))
        except (TypeError, ValueError):
            pass

    # Free-form absolute date – only if it actually looks like one
    if not (MONTH_NAME_RE.search(s) or NUMERIC_DATE_RE.search(s)):
        return None
    try:
        default = now.astimezone(default_tz).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
        dt = du_parser.parse(s, default=default, dayfirst=dayfirst, fuzzy=True, tzinfos=TZINFOS)
    except (ValueError, OverflowError, TypeError):
        return None
    dt = _to_utc(dt, default_tz)
    # missing year: "Dec 31" seen on Jan 2 belongs to last year
    if not YEAR_RE.search(s) and dt > now + timedelta(days=1):
        dt = dt.replace(year=dt.year - 1)
    date_only = not HAS_TIME_RE.search(s)
    return ParsedDate(dt, date_only=date_only, granularity_s=86400 if date_only else 0)


# ====================================================================== page-level extraction
JSONLD_KEYS = ("datePublished", "datePosted", "dateCreated", "uploadDate")
META_SELECTORS = (
    ('meta[property="article:published_time"]', "meta:article:published_time"),
    ('meta[property="og:published_time"]', "meta:og:published_time"),
    ('meta[name="article:published_time"]', "meta:article:published_time"),
    ('meta[itemprop="datePublished"]', "meta:itemprop:datePublished"),
    ('meta[name="pubdate"]', "meta:pubdate"),
    ('meta[name="publish-date"]', "meta:publish-date"),
    ('meta[name="publish_date"]', "meta:publish_date"),
    ('meta[name="parsely-pub-date"]', "meta:parsely-pub-date"),
    ('meta[name="sailthru.date"]', "meta:sailthru.date"),
    ('meta[name="dc.date"]', "meta:dc.date"),
    ('meta[name="DC.date.issued"]', "meta:dc.date.issued"),
    ('meta[name="date"]', "meta:date"),
)
BYLINE_SELECTORS = (
    '[class*="date"]', '[class*="time"]', '[class*="posted"]', '[class*="publish"]',
    '[class*="byline"]', '[class*="meta"]', '[data-testid*="date"]',
)


def _walk_jsonld(obj, out: list[tuple[str, str]], depth: int = 0) -> None:
    if depth > 6:
        return
    if isinstance(obj, list):
        for x in obj:
            _walk_jsonld(x, out, depth + 1)
    elif isinstance(obj, dict):
        for k in JSONLD_KEYS:
            if isinstance(obj.get(k), (str, int)):
                out.append((k, str(obj[k])))
        for k in ("@graph", "mainEntity", "mainEntityOfPage", "itemListElement", "item"):
            if k in obj:
                _walk_jsonld(obj[k], out, depth + 1)


def collect_candidates(
    html: str,
    url: str | None = None,
    *,
    headers: dict | None = None,
    now: datetime | None = None,
    default_tz=UTC,
    dayfirst: bool = False,
) -> list[DateResult]:
    now = now or datetime.now(UTC)
    tree = LexborHTMLParser(html)
    out: list[DateResult] = []

    def add(raw: str | None, source: str, conf: float) -> None:
        if not raw:
            return
        p = parse_date(raw, now, default_tz=default_tz, dayfirst=dayfirst)
        if p:
            out.append(DateResult(p.dt, source, conf, raw.strip()[:80], p.date_only, p.relative, p.granularity_s))

    for node in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.text() or "")
        except ValueError:
            continue
        found: list[tuple[str, str]] = []
        _walk_jsonld(data, found)
        for key, raw in found:
            add(raw, f"jsonld:{key}", 0.95 if key in ("datePublished", "datePosted") else 0.8)

    for sel, label in META_SELECTORS:
        if (node := tree.css_first(sel)) and (v := node.attributes.get("content")):
            add(v, label, 0.90)

    for node in tree.css("time")[:10]:
        attrs = node.attributes
        cls = (attrs.get("class") or "").lower()
        if "modif" in cls or "updated" in cls:
            continue
        conf = 0.85 if (attrs.get("itemprop") == "datePublished" or "pubdate" in attrs or "publish" in cls) else 0.8
        add(attrs.get("datetime") or node.text(strip=True), "time-tag", conf)

    if url and (m := URL_DATE_RE.search(url)):
        add("-".join(m.groups()), "url", 0.60)

    # Visible byline text – catches "Posted 3 hours ago" on job boards.
    seen_text: set[str] = set()
    for sel in BYLINE_SELECTORS:
        for node in tree.css(sel)[:15]:
            text = node.text(strip=True)
            if not text or len(text) > 80 or text in seen_text:
                continue
            seen_text.add(text)
            low = text.lower()
            if "updated" in low or "modified" in low:
                continue
            if (RELATIVE_RE.search(text) or COMPACT_RE.match(text) or DAYWORD_RE.search(text)
                    or YEAR_RE.search(text)):
                add(text, "text:byline", 0.55)

    if headers and (lm := headers.get("last-modified") or headers.get("Last-Modified")):
        add(lm, "http:last-modified", 0.30)
    return out


def best_candidate(cands: list[DateResult], now: datetime | None = None) -> DateResult | None:
    """Sanity-check candidates and pick the most reliable one."""
    now = now or datetime.now(UTC)
    valid: list[DateResult] = []
    for c in cands:
        if c.dt.year < 1995:
            continue
        if c.dt > now + timedelta(hours=1):
            # up to 14 h ahead = a timezone mistake (local time labelled UTC); clamp
            if c.dt <= now + timedelta(hours=14):
                c.dt, c.confidence = now, c.confidence - 0.2
            else:
                continue
        valid.append(c)
    if not valid:
        return None
    valid.sort(key=lambda c: (c.confidence, not c.date_only), reverse=True)
    best = valid[0]
    # Corroboration: the strongest candidates disagreeing by > 2 days is a red flag.
    strong = [c for c in valid[1:] if c.confidence >= 0.6 and c.source != best.source]
    if strong and abs((strong[0].dt - best.dt).total_seconds()) > 2 * 86400:
        best.confidence = round(best.confidence - 0.25, 2)
    elif strong:
        best.confidence = round(min(0.99, best.confidence + 0.03), 2)
    return best


def extract_published(
    html: str,
    url: str | None = None,
    *,
    headers: dict | None = None,
    now: datetime | None = None,
    default_tz=UTC,
    dayfirst: bool = False,
) -> DateResult | None:
    now = now or datetime.now(UTC)
    cands = collect_candidates(html, url, headers=headers, now=now, default_tz=default_tz, dayfirst=dayfirst)
    return best_candidate(cands, now)


def tz_from_name(name: str | None):
    """'Asia/Kolkata' -> tzinfo, for sources whose naive dates are local time."""
    return du_tz.gettz(name) if name else UTC
