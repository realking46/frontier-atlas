from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class SourceStats:
    source: str
    listed: int = 0
    fresh: int = 0
    stale: int = 0
    duplicate: int = 0
    uncertain: int = 0
    not_ai: int = 0
    near_duplicate: int = 0
    blocked: int = 0              # pages behind captcha / hard block (see core/protected.py)
    stored: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["errors"] = d["errors"][:5]
        return d
