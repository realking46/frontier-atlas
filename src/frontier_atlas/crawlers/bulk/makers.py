"""Who makes this product? Deterministic, evidence-based maker lookup.

Directories like Futurepedia list a product's name, URL and pricing but not
the company behind it. Instead of guessing, we read the maker from the
product's OWN website, in order of strength:

    1. domain match   the product's domain is a startup we already hold (YC)   -> that startup
    2. json-ld        schema.org Organization / publisher / author name on the site
    3. copyright      "(c) 2025 Acme Labs, Inc." in the page footer
    4. og:site_name   the site's declared name
    5. title=domain   brand segment of <title>, only when it IS the site's domain name

Every value is a string that literally appears on the evidence page, whose URL
is stored next to it (`startupEvidenceUrl`), so each maker name is auditable.
Nothing found -> the maker stays empty; we never invent one.
"""
from __future__ import annotations

import html as htmllib
import json
import re
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse

from selectolax.lexbor import LexborHTMLParser

# hosts where the URL belongs to a platform, not to the maker
PLATFORM_HOSTS = (
    "apps.apple.com", "play.google.com", "chrome.google.com", "chromewebstore.google.com", "github.com",
    "huggingface.co", "discord.gg", "discord.com", "producthunt.com", "notion.site", "linktr.ee",
    "medium.com", "substack.com", "twitter.com", "x.com", "youtube.com", "linkedin.com", "facebook.com",
    "instagram.com", "t.me", "telegram.me", "microsoft.com", "microsoftedge.microsoft.com", "addons.mozilla.org",
    "wordpress.org", "marketplace.visualstudio.com", "apps.shopify.com", "workspace.google.com", "gumroad.com",
)
# names that are site builders / hosts / boilerplate, never a maker
JUNK_NAMES = {
    "webflow", "framer", "wix", "wix.com", "squarespace", "shopify", "wordpress", "vercel", "netlify", "bubble",
    "google", "apple", "home", "homepage", "website", "my website", "site", "all rights reserved", "copyright",
    "futurepedia", "carrd", "softr", "notion", "hubspot", "godaddy", "cloudflare", "react app", "vite app",
    "untitled", "index", "welcome", "loading", "app", "beta", "login", "sign in", "inc", "llc", "ltd",
    "google docs", "google sites", "google drive", "google forms", "medium", "substack", "github", "youtube",
    "linkedin", "typeform", "canva", "wix website", "squarespace website", "notion site", "gitbook", "ghost",
}
# Footer link text the copyright pattern can run into ("RivalFlow, Inc Terms Privacy").
# The name is cut at the first such word; a name that STARTS with one is rejected.
# Bare "policy" / "made with" are not in the list: "Climate Policy Radar" and
# "Made With Intent Limited" are real companies.
# Words that also occur inside real names ("Eve Legal", "Video To Blog") are deliberately absent.
_FOOTER_WORD = (r"(?:privacy|terms|cookies?|cookie settings|manage cookies|sitemap|contact us|about us|"
                r"impressum|imprint|disclaimer|support|powered by|designed by)")
_FOOTER_TAIL_RE = re.compile(r"\s+" + _FOOTER_WORD + r"\b.*$", re.I)
_FOOTER_HEAD_RE = re.compile(r"^" + _FOOTER_WORD + r"\b", re.I)
TWO_LEVEL_TLDS = {"co.uk", "co.jp", "com.au", "co.in", "com.br", "co.kr", "com.sg", "co.nz", "com.cn", "org.uk"}

_MARK = r"(?:©|&copy;|\(c\)|\bcopyright\b)"
_COPYRIGHT_RE = re.compile(
    _MARK + r"(?:\s*(?:" + _MARK + r"|(?:19|20)\d{2}|present|[-–—,]|by\b))*\s*"
    r"([A-Z0-9][\w&.,'’\- ]{1,70}?"
    r"(?:,?\s+(?:Inc|Ltd|Pty|Co|Corp|LLC|L\.L\.C|GmbH|S\.A|B\.V|AG|SAS|Limited|Pvt|PBC)\.?)*)"
    r"(?=\s*(?:[.|·•]\s|[©®™]|all rights|\(|$|\s{2,}|\.$|,\s*all rights))",
    re.I,
)
_ORG_TYPES = {"organization", "corporation", "localbusiness", "onlinebusiness", "ngo", "educationalorganization"}
_TITLE_SPLIT = re.compile(r"\s+[|:\-–—·•]\s+|:\s")


@dataclass(frozen=True)
class Maker:
    name: str
    method: str
    evidence_url: str


def clean_url(url: str) -> str:
    """Drop query string / fragment (directories append utm tracking)."""
    p = urlparse(url.strip())
    return urlunparse((p.scheme or "https", p.netloc, p.path or "/", "", "", ""))


def registrable_domain(url_or_host: str | None) -> str | None:
    if not url_or_host:
        return None
    host = urlparse(url_or_host).netloc if "://" in url_or_host else url_or_host
    host = host.lower().split(":")[0].removeprefix("www.")
    parts = host.split(".")
    if len(parts) < 2:
        return None
    n = 3 if ".".join(parts[-2:]) in TWO_LEVEL_TLDS else 2
    return ".".join(parts[-n:])


def is_platform(url: str) -> bool:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    return any(host == h or host.endswith("." + h) for h in PLATFORM_HOSTS)


def _alnum(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _plausible(name: str | None) -> str | None:
    if not name:
        return None
    n = htmllib.unescape(re.sub(r"\s+", " ", name)).strip(" .,-|'\"·•–—")
    # "all rights reserved" in English / Chinese / Japanese / Korean, and anything after it
    n = re.sub(r"(?i)\s*(?:all rights reserved|版权所有|著作権|저작권).*$", "", n)
    n = re.sub(r"^(?:19|20)\d{2}\s+", "", n.strip(" .,-|")).strip(" .,-|")         # leading year
    if _FOOTER_HEAD_RE.match(n) or re.match(r"(?i)made with\b", n) and len(n.split()) <= 3:
        return None
    n = _FOOTER_TAIL_RE.sub("", n)
    n = re.sub(r"\s+(?:19|20)\d{2}$", "", n).strip(" .,-|")           # "Playful Software 2026"
    if not (2 <= len(n) <= 60) or n.lower() in JUNK_NAMES:
        return None
    if re.fullmatch(r"[\d\W_]+", n) or "{" in n or "http" in n.lower():
        return None
    if len(n.split()) > 7:
        return None
    return n


def _jsonld_blocks(tree: LexborHTMLParser) -> list[dict]:
    out: list[dict] = []
    for node in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.text() or "")
        except (json.JSONDecodeError, ValueError):
            continue
        stack = [data]
        while stack:
            x = stack.pop()
            if isinstance(x, list):
                stack.extend(x)
            elif isinstance(x, dict):
                out.append(x)
                if "@graph" in x:
                    stack.append(x["@graph"])
    return out


def _types(d: dict) -> set[str]:
    t = d.get("@type")
    return {s.lower() for s in (t if isinstance(t, list) else [t]) if isinstance(s, str)}


def _name_of(v) -> str | None:
    if isinstance(v, list):
        v = v[0] if v else None
    if isinstance(v, dict):
        return v.get("legalName") or v.get("name")
    return v if isinstance(v, str) else None


def from_jsonld(tree: LexborHTMLParser) -> str | None:
    blocks = _jsonld_blocks(tree)
    for d in blocks:                                     # an explicit Organization first
        if _types(d) & _ORG_TYPES and (n := _plausible(d.get("legalName") or d.get("name"))):
            return n
    for d in blocks:                                     # then who publishes / authors the app or site
        for k in ("publisher", "author", "creator", "provider", "manufacturer", "brand"):
            if (n := _plausible(_name_of(d.get(k)))):
                return n
    return None


def from_copyright(text: str) -> str | None:
    # the footer (end of page) first, where the copyright line lives
    for chunk in (text[-3000:], text):
        for m in _COPYRIGHT_RE.finditer(chunk):
            if (n := _plausible(m.group(1))):
                return n
    return None


def from_og(tree: LexborHTMLParser) -> str | None:
    node = tree.css_first('meta[property="og:site_name"]') or tree.css_first('meta[name="application-name"]')
    return _plausible(node.attributes.get("content") if node else None)


def from_title(tree: LexborHTMLParser, url: str) -> str | None:
    """Brand segment of <title> ("Gencraft: AI art generator"), accepted ONLY when it
    is the site's own domain name (gencraft.com): then the site owner is that brand."""
    node = tree.css_first("title")
    dom = registrable_domain(url)
    if not node or not dom:
        return None
    label = _alnum(dom.split(".")[0])
    for seg in _TITLE_SPLIT.split(node.text() or ""):
        n = _plausible(seg)
        if n and _alnum(n) == label:
            return n
    return None


def maker_from_html(page_html: str, url: str) -> Maker | None:
    tree = LexborHTMLParser(page_html)
    if (n := from_jsonld(tree)):
        return Maker(n, "website:json-ld", url)
    og = from_og(tree)
    title = from_title(tree, url)
    for node in tree.css("script, style, noscript, template"):
        node.decompose()
    body = tree.body
    text = re.sub(r"\s+", " ", body.text(separator=" ")) if body else ""
    if (n := from_copyright(text)):
        return Maker(n, "website:copyright", url)
    if og:
        return Maker(og, "website:og:site_name", url)
    if title:
        return Maker(title, "website:title=domain", url)
    return None
