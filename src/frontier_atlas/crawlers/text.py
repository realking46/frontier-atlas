"""Main-content extraction: article / job page HTML -> clean full text.

trafilatura (boilerplate-removal tuned on news pages) is the primary
extractor; our own `html_to_text` is the fallback for pages it rejects
(very short pages, unusual markup).
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass

from selectolax.lexbor import LexborHTMLParser

from ..extraction.chunker import html_to_text


@dataclass(slots=True)
class ArticleText:
    text: str
    title: str | None = None
    author: str | None = None
    description: str | None = None
    extractor: str = "trafilatura"


def _get(doc, key):
    if doc is None:
        return None
    if isinstance(doc, dict):
        return doc.get(key)
    return getattr(doc, key, None)


def extract_article(html: str, url: str | None = None) -> ArticleText:
    try:
        import trafilatura

        doc = trafilatura.bare_extraction(
            html, url=url, with_metadata=True, include_comments=False, include_tables=False,
            favor_precision=True,
        )
    except Exception:  # trafilatura raises on some malformed docs
        doc = None
    text = (_get(doc, "text") or "").strip()
    if len(text) >= 200:
        return ArticleText(
            text=text,
            title=_get(doc, "title"),
            author=_get(doc, "author"),
            description=_get(doc, "description"),
        )
    # fallback: our boilerplate stripper, minus the metadata header lines
    fallback = "\n".join(l for l in html_to_text(html).splitlines() if not l.startswith("["))
    return ArticleText(text=fallback.strip(), title=_get(doc, "title"), author=_get(doc, "author"),
                       description=_get(doc, "description"), extractor="fallback")


async def extract_article_async(html: str, url: str | None = None) -> ArticleText:
    """CPU-bound parsing off the event loop, so fetches keep flowing."""
    return await asyncio.to_thread(extract_article, html, url)


def strip_html(fragment: str | None, limit: int | None = None) -> str:
    """HTML snippet (RSS description, API job description) -> plain text."""
    if not fragment:
        return ""
    if "<" in fragment:
        tree = LexborHTMLParser(f"<div>{fragment}</div>")
        for bad in tree.css("script, style"):
            bad.decompose()
        text = tree.body.text(separator=" ") if tree.body else fragment
    else:
        text = fragment
    text = re.sub(r"\s+", " ", text).strip()
    if limit and len(text) > limit:
        text = text[: limit].rsplit(" ", 1)[0] + "…"
    return text
