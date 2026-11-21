"""HTML -> clean text -> budget-fitted payload.

Goal: a request must *never* trigger 413 / context-overflow, yet keep the
information the extractor actually needs.

Strategy ("head + densest blocks"):
  1. Strip boilerplate (scripts, nav, footers, cookie banners …).
  2. Lift high-value metadata to the top: <title>, meta description,
     OpenGraph/article dates, <time datetime>, JSON-LD (often contains the
     exact org name, datePosted, employee count …).
  3. If everything fits the token budget, send it all.
  4. Otherwise always keep the head (metadata + opening blocks, ~30% budget),
     score remaining blocks for semantic density (schema keywords, numbers,
     dates, proper nouns; penalise link-farm / menu lines), take the best
     blocks greedily, then restore original order so the text still reads
     naturally.
  5. Enforce a hard byte cap as well, because some gateways reject on bytes
     before tokens are ever counted.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache

from selectolax.lexbor import LexborHTMLParser as HTMLParser

JUNK_TAGS = (
    "script", "style", "noscript", "svg", "iframe", "form", "nav", "footer",
    "header", "aside", "button", "select", "canvas", "template",
)
JUNK_ATTR_RE = re.compile(r"cookie|consent|newsletter|subscribe|share|social|breadcrumb|sidebar|advert|promo|popup|modal", re.I)
BOILERPLATE_LINE_RE = re.compile(
    r"^(sign in|log in|sign up|subscribe|accept( all)? cookies|share|tweet|menu|home|"
    r"privacy policy|terms( of service)?|all rights reserved|©.*|skip to content|read more|advertisement)$",
    re.I,
)
DATE_RE = re.compile(
    r"\b(\d{4}-\d{2}-\d{2}|\d{1,2} (jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)|"
    r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]* \d{1,2}|\d+ (minute|hour|day|week)s? ago|yesterday|today)\b",
    re.I,
)

# Words that signal the block holds schema-relevant facts, per extraction type.
KEYWORDS: dict[str, tuple[str, ...]] = {
    "startup": ("founded", "employees", "team size", "headquarter", "hq", "funding", "raised",
                "series", "seed", "investors", "ceo", "founder", "website", "location", "industry"),
    "product": ("pricing", "free", "freemium", "trial", "per month", "/mo", "enterprise", "plan",
                "launch", "features", "made by", "maker", "company", "api"),
    "job": ("remote", "hybrid", "on-site", "onsite", "location", "posted", "ago", "salary",
            "apply", "responsibilities", "requirements", "engineer", "full-time", "contract"),
    "news": ("said", "announced", "according", "published", "updated", "by ", "reported",
             "launch", "raised", "billion", "million"),
}


@dataclass(slots=True)
class ChunkResult:
    text: str
    tokens: int
    original_tokens: int
    truncated: bool
    kept_blocks: int
    total_blocks: int


# ---------------------------------------------------------------- tokens
@lru_cache(maxsize=1)
def _encoder():
    try:
        import tiktoken

        return tiktoken.get_encoding("cl100k_base")
    except Exception:  # offline / not installed -> heuristic
        return None


def count_tokens(text: str) -> int:
    """cl100k is not every provider's tokenizer, so we add a 10% safety margin."""
    enc = _encoder()
    n = len(enc.encode(text, disallowed_special=())) if enc else len(text) // 4 + 1
    return int(n * 1.10) + 1


# ---------------------------------------------------------------- html -> text
def html_to_text(html: str) -> str:
    tree = HTMLParser(html)
    meta_lines = _extract_metadata(tree)

    for tag in JUNK_TAGS:
        for node in tree.css(tag):
            node.decompose()
    for node in tree.css("[class], [id]"):
        attrs = f"{node.attributes.get('class') or ''} {node.attributes.get('id') or ''}"
        # only drop small containers – a wrapper div named "share-wrapper" may hold the article
        if JUNK_ATTR_RE.search(attrs) and len(node.text(strip=True)) < 400:
            node.decompose()

    root = tree.body or tree.root
    body = root.text(separator="\n") if root else ""
    return "\n".join(meta_lines + [""] + _clean_lines(body)).strip()


def _extract_metadata(tree: HTMLParser) -> list[str]:
    lines: list[str] = []
    if (t := tree.css_first("title")) and t.text(strip=True):
        lines.append(f"[title] {t.text(strip=True)}")
    for sel, label in (
        ('meta[name="description"]', "description"),
        ('meta[property="og:title"]', "og:title"),
        ('meta[property="og:site_name"]', "site"),
        ('meta[property="article:published_time"]', "published_time"),
        ('meta[property="article:modified_time"]', "modified_time"),
        ('meta[name="date"]', "date"),
        ('meta[itemprop="datePublished"]', "datePublished"),
    ):
        node = tree.css_first(sel)
        if node and (val := node.attributes.get("content")):
            lines.append(f"[{label}] {val.strip()}")
    for node in tree.css("time[datetime]")[:5]:
        lines.append(f"[time] {node.attributes.get('datetime')} ({node.text(strip=True)})")
    for node in tree.css('script[type="application/ld+json"]')[:3]:
        try:
            data = json.loads(node.text() or "")
        except ValueError:
            continue
        compact = json.dumps(_prune_jsonld(data), ensure_ascii=False, separators=(",", ":"))
        lines.append(f"[json-ld] {compact[:3000]}")
    return lines


def _prune_jsonld(data, depth: int = 0):
    """Drop bulky, low-value JSON-LD keys (images, long bodies)."""
    if depth > 4:
        return None
    if isinstance(data, dict):
        return {
            k: _prune_jsonld(v, depth + 1)
            for k, v in data.items()
            if k not in {"image", "logo", "articleBody", "potentialAction", "thumbnailUrl", "@context"}
        }
    if isinstance(data, list):
        return [_prune_jsonld(x, depth + 1) for x in data[:10]]
    return data


def _clean_lines(text: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in text.splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if not line or BOILERPLATE_LINE_RE.match(line):
            continue
        key = line.lower()
        if key in seen:  # repeated menus / footers
            continue
        seen.add(key)
        out.append(line)
    return out


# ---------------------------------------------------------------- blocks + scoring
def _to_blocks(text: str, max_block_chars: int = 1200) -> list[str]:
    """Merge very short lines into paragraphs, split very long ones."""
    blocks: list[str] = []
    buf = ""
    for line in text.splitlines():
        if not line.strip():
            if buf:
                blocks.append(buf)
                buf = ""
            continue
        if line.startswith("[") and "]" in line[:20]:  # metadata line stays on its own
            if buf:
                blocks.append(buf)
                buf = ""
            blocks.append(line)
            continue
        if buf and (len(line) >= 80 or len(buf) >= 400):
            blocks.append(buf)
            buf = line
        else:
            buf = f"{buf} {line}".strip()
        if len(buf) > max_block_chars:
            for i in range(0, len(buf), max_block_chars):
                blocks.append(buf[i : i + max_block_chars])
            buf = ""
    if buf:
        blocks.append(buf)
    return [b for b in blocks if b]


def score_block(block: str, kind: str) -> float:
    low = block.lower()
    words = block.split()
    n = max(len(words), 1)
    score = 0.0
    score += 3.0 * sum(1 for kw in KEYWORDS.get(kind, ()) if kw in low)
    score += 2.0 * len(DATE_RE.findall(block))
    score += min(3.0, 0.5 * len(re.findall(r"\d", block)) / 4)
    caps = sum(1 for w in words if w[:1].isupper())
    score += 4.0 * (caps / n) if n > 4 else 0
    # Prefer prose-sized blocks; penalise one-word menu items and walls of links.
    if n < 4:
        score -= 2.0
    if low.count("http") > 3 or block.count("|") > 4:
        score -= 3.0
    return score


# ---------------------------------------------------------------- public API
def fit_to_budget(
    text: str,
    max_tokens: int,
    *,
    kind: str = "",
    max_bytes: int | None = None,
    head_ratio: float = 0.30,
) -> ChunkResult:
    original_tokens = count_tokens(text)
    blocks = _to_blocks(text)
    if original_tokens <= max_tokens and (max_bytes is None or len(text.encode()) <= max_bytes):
        return ChunkResult(text, original_tokens, original_tokens, False, len(blocks), len(blocks))

    costs = [count_tokens(b) for b in blocks]
    keep: set[int] = set()
    used = 0

    # 1) head: metadata + opening blocks
    head_budget = int(max_tokens * head_ratio)
    for i, c in enumerate(costs):
        if used + c > head_budget:
            break
        keep.add(i)
        used += c

    # 2) densest remaining blocks
    ranked = sorted(
        (i for i in range(len(blocks)) if i not in keep),
        key=lambda i: score_block(blocks[i], kind) / max(costs[i], 1) ** 0.5,
        reverse=True,
    )
    for i in ranked:
        if used + costs[i] <= max_tokens:
            keep.add(i)
            used += costs[i]

    out = "\n".join(blocks[i] for i in sorted(keep))

    # 3) a single huge first block can still be too big -> hard character cut
    if count_tokens(out) > max_tokens or not out:
        approx_chars = max_tokens * 3
        out = (out or text)[:approx_chars]
        while count_tokens(out) > max_tokens and len(out) > 100:
            out = out[: int(len(out) * 0.9)]

    if max_bytes is not None:
        raw = out.encode()
        if len(raw) > max_bytes:
            out = raw[:max_bytes].decode("utf-8", errors="ignore")

    return ChunkResult(out, count_tokens(out), original_tokens, True, len(keep), len(blocks))
