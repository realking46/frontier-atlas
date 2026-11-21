"""Anti-hallucination gate.

Every field marked `grounded` in an extraction schema must be traceable to the
source text. Values that are not found are nulled out and reported; if the
record's identity field (company name, job title, headline …) is not found,
the whole extraction is rejected – we would rather lose a record than ship a
made-up one.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from urllib.parse import urlparse

from rapidfuzz import fuzz

from ..schemas.base import ExtractionContent

# identity field per extraction type: if this is ungrounded the record is rejected
IDENTITY_FIELD = {
    "startup": "entityName",
    "product": "productName",
    "job": "title",
    "news": "title",
}


@dataclass(slots=True)
class GroundingReport:
    ok: bool
    dropped: list[str] = field(default_factory=list)
    reason: str = ""


def normalize(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).lower()
    s = re.sub(r"[‘’“”]", "'", s)
    s = re.sub(r"(?<=\d)[,  ](?=\d{3}\b)", "", s)  # 3,000 -> 3000
    s = re.sub(r"[^\w\s@./:'-]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _str_in_source(value: str, norm_source: str, min_score: int) -> bool:
    v = normalize(value)
    if not v:
        return True
    if v in norm_source:
        return True
    if value.startswith(("http://", "https://")) or "." in v and " " not in v:
        host = urlparse(value if "://" in value else f"https://{value}").netloc.lower()
        host = host.removeprefix("www.")
        return bool(host) and host in norm_source
    if len(v) < 4:  # short tokens: fuzzy matching is meaningless, require exact
        return False
    return fuzz.partial_ratio(v, norm_source) >= min_score


def _int_in_source(value: int, norm_source: str) -> bool:
    candidates = {str(value), f"{value:,}"}
    if value >= 1000 and value % 1000 == 0:
        candidates |= {f"{value // 1000}k", f"{value // 1000} k"}
    return any(re.search(rf"(?<![\d.]){re.escape(c)}(?![\d])", norm_source) for c in candidates)


def check(
    extraction: ExtractionContent,
    source_text: str,
    *,
    kind: str,
    min_score: int = 85,
) -> GroundingReport:
    """Mutates `extraction` in place: ungrounded grounded-fields become None."""
    norm_source = normalize(source_text)
    report = GroundingReport(ok=True)

    for name in extraction.grounded_fields():
        value = getattr(extraction, name)
        if value is None or value == "" or value == []:
            continue
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            good = _int_in_source(value, norm_source)
        elif isinstance(value, str):
            good = _str_in_source(value, norm_source, min_score)
        elif isinstance(value, list):
            kept = [v for v in value if not isinstance(v, str) or _str_in_source(v, norm_source, min_score)]
            good = len(kept) == len(value)
            if not good:
                setattr(extraction, name, kept or None)
                report.dropped.append(name)
                continue
        else:
            good = True
        if not good:
            setattr(extraction, name, None)
            report.dropped.append(name)

    identity = IDENTITY_FIELD.get(kind)
    if identity and getattr(extraction, identity, None) in (None, ""):
        report.ok = False
        report.reason = (
            f"identity field '{identity}' missing or not found in source"
            if identity in report.dropped
            else f"identity field '{identity}' missing"
        )
    return report
