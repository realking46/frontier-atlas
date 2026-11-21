from __future__ import annotations

from datetime import datetime
from typing import ClassVar, Literal

from pydantic import BaseModel

from .base import BaseRecord, ExtractionContent, grounded, inferred

ROLE_FAMILIES = (
    "Engineering", "Research", "Data", "Product", "Design",
    "Sales", "Marketing", "Operations", "Other",
)


class JobExtraction(ExtractionContent):
    prompt_name: ClassVar[str] = "job"

    title: str | None = grounded("Job title exactly as written")
    company: str | None = grounded("Hiring company exactly as written")
    location: str | None = grounded("Location as written")
    date_text: str | None = grounded("The posting date text exactly as shown, e.g. '3 hours ago' or 'Nov 20, 2026'")
    is_remote: bool | None = inferred("True if remote work is allowed")
    role_family: str | None = inferred("One of: " + ", ".join(ROLE_FAMILIES))


class JobContent(BaseModel):
    title: str
    company: str
    rawCompany: str | None = None
    date: datetime  # normalised by freshness/date_parser, never by the LLM
    date_source: str | None = None
    is_remote: bool | None = None
    role_family: str | None = None
    location: str | None = None
    ai_keywords: list[str] = []      # why the job counts as an AI job
    extraction: str | None = None    # "structured" | "llm:<provider>" | "rules"


class JobRecord(BaseRecord):
    recordType: Literal["JOB"] = "JOB"
    content: JobContent
