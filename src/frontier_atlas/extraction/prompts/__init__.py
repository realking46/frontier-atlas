"""Prompt building. Per-type instructions live in the *.txt files next to this module."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import get_args

from ...schemas.base import ExtractionContent

_DIR = Path(__file__).parent

SYSTEM_TEMPLATE = """You are a precise information-extraction engine for an AI-ecosystem knowledge graph.
Read the web page text supplied by the user and return ONLY one JSON object with exactly these keys:

{fields}

Hard rules:
- Use null for any value that is not explicitly present in the text. NEVER guess or use outside knowledge.
- Copy names, titles and dates exactly as they are written in the text.
- Integers must be plain numbers (no commas, no ranges; for a range like "51-200" use the upper bound only if it is written).
- Output JSON only: no markdown, no commentary.

{type_instructions}"""


def _type_name(annotation) -> str:
    args = [a for a in get_args(annotation) if a is not type(None)]
    t = args[0] if args else annotation
    if hasattr(t, "__members__"):  # Enum
        return " | ".join(f'"{m}"' for m in t.__members__)
    if getattr(t, "__origin__", None) is list:
        return "array of strings"
    return {str: "string", int: "integer", bool: "boolean", float: "number"}.get(t, str(t))


@lru_cache(maxsize=32)
def system_prompt(schema_cls: type[ExtractionContent]) -> str:
    lines = [
        f'- "{name}": {_type_name(f.annotation)} or null — {f.description or ""}'
        for name, f in schema_cls.model_fields.items()
    ]
    path = _DIR / f"{schema_cls.prompt_name}.txt"
    type_instructions = path.read_text(encoding="utf-8").strip() if path.exists() else ""
    return SYSTEM_TEMPLATE.format(fields="\n".join(lines), type_instructions=type_instructions)


def user_prompt(page_text: str, source_url: str | None = None, feedback: str | None = None) -> str:
    parts = []
    if source_url:
        parts.append(f"SOURCE URL: {source_url}")
    parts.append("PAGE TEXT:\n<<<\n" + page_text + "\n>>>")
    if feedback:
        parts.append(
            "Your previous answer was rejected: " + feedback +
            "\nFix it. Only use values that appear in the PAGE TEXT; use null otherwise."
        )
    return "\n\n".join(parts)


def schema_keys(schema_cls: type[ExtractionContent]) -> list[str]:
    return list(schema_cls.model_fields)


def example_json(schema_cls: type[ExtractionContent]) -> str:
    return json.dumps({k: None for k in schema_cls.model_fields})
