"""Flatten stored records to CSV (one file per Google Sheets tab)."""
from __future__ import annotations

import csv
from collections.abc import Callable, Iterable
from pathlib import Path

from ..schemas.base import _flatten


def write_csv(
    bodies: Iterable[dict],
    path: str | Path,
    *,
    columns: list[str] | None = None,
    where: Callable[[dict], bool] | None = None,
) -> int:
    rows = [_flatten(b) for b in bodies if where is None or where(b)]
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if columns is None:
        columns = list(dict.fromkeys(k for r in rows for k in r))
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return len(rows)


PAPER_COLUMNS = [
    "schemaVersion", "recordType", "content.title", "content.authors", "content.paper_url",
    "content.github_url", "content.github_stars", "content.github_stars_source",
    "content.github_stars_checked_at", "content.published_date", "content.arxiv_id",
    "source.name", "source.url", "content.sources", "collectedAt",
]
