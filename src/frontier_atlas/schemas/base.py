"""Canonical record envelope shared by every entity type.

Field names follow the assessment spec exactly (mixed camelCase/snake_case
is intentional – it is what the spec asks for).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

SCHEMA_VERSION = "1.0"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def grounded(description: str = "", **kw: Any) -> Any:
    """Field whose value must literally appear in the source text (checked by grounding.py)."""
    return Field(default=None, description=description, json_schema_extra={"grounded": True}, **kw)


def inferred(description: str = "", **kw: Any) -> Any:
    """Field the model may infer/classify (enums, booleans, categories)."""
    return Field(default=None, description=description, json_schema_extra={"grounded": False}, **kw)


class Source(BaseModel):
    name: str
    url: HttpUrl


class ExtractionContent(BaseModel):
    """Base for what the LLM is asked to return. Unknown keys are dropped."""

    model_config = ConfigDict(extra="ignore")

    # Name of the prompt template in extraction/prompts/
    prompt_name: ClassVar[str] = ""

    @classmethod
    def grounded_fields(cls) -> list[str]:
        out = []
        for name, f in cls.model_fields.items():
            extra = f.json_schema_extra if isinstance(f.json_schema_extra, dict) else {}
            if extra.get("grounded"):
                out.append(name)
        return out


class BaseRecord(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    schemaVersion: str = SCHEMA_VERSION
    recordType: str
    source: Source
    collectedAt: datetime = Field(default_factory=utcnow)

    def to_row(self) -> dict[str, Any]:
        """Flatten to dotted keys for CSV / Google Sheets export."""
        return _flatten(self.model_dump(mode="json"))


def _flatten(d: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, key + "."))
        elif isinstance(v, list):
            out[key] = "; ".join(str(x) for x in v)
        else:
            out[key] = v
    return out
