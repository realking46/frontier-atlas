"""Research papers come from structured APIs (arXiv, GitHub), not the LLM,
so there is no extraction model here – only the canonical record."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from .base import BaseRecord


class PaperContent(BaseModel):
    title: str
    authors: list[str]
    paper_url: str
    github_url: str | None = None
    github_stars: int | None = None
    github_stars_source: str | None = None     # "github_api" (live) | "huggingface" (source-reported)
    github_stars_checked_at: datetime | None = None
    github_repo_exists: bool | None = None
    published_date: datetime | None = None
    arxiv_id: str | None = None
    abstract: str | None = None
    sources: list[str] = []                    # every source that listed this paper


class PaperRecord(BaseRecord):
    recordType: Literal["RESEARCH_PAPER"] = "RESEARCH_PAPER"
    content: PaperContent
