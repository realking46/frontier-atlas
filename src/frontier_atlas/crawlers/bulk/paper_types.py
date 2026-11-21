"""Intermediate representation shared by all paper sources before merging."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

ARXIV_ID_RE = re.compile(r"(\d{4}\.\d{4,5})(v\d+)?")


def arxiv_id_from(text: str | None) -> str | None:
    """Version-less new-style arXiv id from a URL or string ('2305.10403v2' -> '2305.10403')."""
    if not text:
        return None
    m = ARXIV_ID_RE.search(text)
    return m.group(1) if m else None


@dataclass(slots=True)
class PaperCandidate:
    source_name: str           # "huggingface" | "arxiv" | "paperswithcode"
    source_url: str            # page/API URL the facts came from (traceability)
    title: str
    authors: list[str]
    arxiv_id: str | None = None
    abstract: str | None = None
    published: datetime | None = None
    paper_url: str | None = None
    github_url: str | None = None
    github_stars: int | None = None          # as reported by the source, if any
    repo_candidates: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        if self.arxiv_id:
            return f"arxiv:{self.arxiv_id}"
        return "title:" + re.sub(r"[^a-z0-9]+", "", self.title.lower())[:120]
