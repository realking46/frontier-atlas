from __future__ import annotations

from datetime import datetime
from typing import ClassVar, Literal

from pydantic import BaseModel

from .base import BaseRecord, ExtractionContent, grounded, inferred


class NewsExtraction(ExtractionContent):
    prompt_name: ClassVar[str] = "news"

    title: str | None = grounded("Headline exactly as written")
    author: str | None = grounded("Author name if shown")
    date_text: str | None = grounded("Publication date text exactly as shown")
    summary: str | None = inferred("2-3 sentence factual summary of the article")
    companies: list[str] | None = inferred("Companies mentioned in the article")


class NewsContent(BaseModel):
    title: str
    published_date: datetime
    date_source: str | None = None        # e.g. "jsonld:datePublished", "rss:pubDate"
    date_confidence: float | None = None
    author: str | None = None
    summary: str | None = None            # source-provided description / dek
    full_text: str | None = None
    text_source: str | None = None        # "article-page" (full text) | "feed-lead" (page blocked; publisher's feed lead only)
    word_count: int | None = None
    companies: list[str] = []


class NewsRecord(BaseRecord):
    recordType: Literal["NEWS"] = "NEWS"
    content: NewsContent
