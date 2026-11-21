from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import BaseModel, Field

from .base import BaseRecord, ExtractionContent, grounded, inferred


class StartupExtraction(ExtractionContent):
    """Shape the LLM must return for a startup page."""

    prompt_name: ClassVar[str] = "startup"

    entityName: str | None = grounded("Company name exactly as written on the page")
    description: str | None = inferred("One-sentence description, paraphrase allowed")
    website: str | None = grounded("Company website URL if shown")
    employeeCount: int | None = grounded("Number of employees if stated (integer)")
    foundedYear: int | None = grounded("Year founded if stated")
    location: str | None = grounded("HQ city/country if stated")
    industries: list[str] | None = inferred("Industry tags")


class StartupData(BaseModel):
    employeeCount: int | None = None
    description: str | None = None
    website: str | None = None
    foundedYear: int | None = None
    location: str | None = None
    country: str | None = None
    industries: list[str] = Field(default_factory=list)
    status: str | None = None          # Active / Acquired / Inactive / Public (as the source states it)
    batch: str | None = None           # accelerator batch, e.g. "Winter 2024"
    linkedin: str | None = None
    founders: list[str] = Field(default_factory=list)


class StartupContent(BaseModel):
    entityName: str
    rawName: str | None = None  # pre-resolution name, kept for the mapping log
    data: StartupData = Field(default_factory=StartupData)


class StartupRecord(BaseRecord):
    recordType: Literal["STARTUP"] = "STARTUP"
    content: StartupContent
