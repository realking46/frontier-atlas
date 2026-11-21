"""Google Sheets deliverable: the six tabs the brief asks for.

    Startups | Products | Research Papers | Jobs | News | Entity Mapping Log

Two outputs from ONE loader, so the sheet always equals the exported CSVs:

  * `write_xlsx()`        -> data/output/frontier_atlas.xlsx. Google Sheets imports it in one
                             step (File > Import > Upload > "Replace spreadsheet"); no credentials.
  * `push_to_sheet()`     -> writes the tabs straight into an existing Google Sheet through the
                             Sheets API v4 with a service account. Re-run after each crawl.

Cell hygiene, applied to both:
  * types restored per column (numbers, booleans) so the sheet sorts and filters properly;
  * text longer than the cell limit (Sheets 50,000 chars, Excel 32,767) is cut with a marker
    pointing to the record's source URL;
  * values that look like formulas ("=...", "+...") are always written as TEXT
    (xlsx string cells, Sheets valueInputOption=RAW): scraped strings must never execute.

Tabs the pipeline does not own are left untouched; only the six tabs are cleared and rewritten
(plus the new spreadsheet's default "Sheet1", removed only when it is verifiably empty).
"""
from __future__ import annotations

import asyncio
import csv
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from ..core.http import HttpFetcher
from ..core.logging import get_logger

log = get_logger(__name__)
csv.field_size_limit(1 << 30)

SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
XLSX_CELL_LIMIT = 32_767
SHEETS_CELL_LIMIT = 50_000
TRUNC_MARK = " ... [truncated; full text at the record's source.url]"


@dataclass(frozen=True)
class Tab:
    title: str
    csv_name: str
    ints: frozenset[str] = frozenset()
    floats: frozenset[str] = frozenset()
    bools: frozenset[str] = frozenset()


TABS: tuple[Tab, ...] = (
    Tab("Startups", "startups.csv", ints=frozenset({"content.data.employeeCount", "content.data.foundedYear"})),
    Tab("Products", "products.csv"),
    Tab("Research Papers", "research_papers.csv", ints=frozenset({"content.github_stars"}),
        bools=frozenset({"content.github_repo_exists"})),
    Tab("Jobs", "jobs.csv", bools=frozenset({"content.is_remote"})),
    Tab("News", "news.csv", ints=frozenset({"content.word_count"}), floats=frozenset({"content.date_confidence"})),
    Tab("Entity Mapping Log", "entity_mapping_log.csv", ints=frozenset({"occurrences"}),
        floats=frozenset({"score"}), bools=frozenset({"is_seed_entity"})),
)


@dataclass
class TabData:
    title: str
    header: list[str]
    rows: list[list] = field(default_factory=list)
    truncated_cells: int = 0


def _typed(tab: Tab, col: str, v: str):
    if v == "":
        return None
    try:
        if col in tab.ints:
            return int(float(v))
        if col in tab.floats:
            return float(v)
    except ValueError:
        return v
    if col in tab.bools and v in ("True", "False", "true", "false"):
        return v.lower() == "true"
    return v


def _fit(v, limit: int) -> tuple[object, bool]:
    if isinstance(v, str) and len(v) > limit:
        return v[: limit - len(TRUNC_MARK)] + TRUNC_MARK, True
    return v, False


def load_tabs(out_dir: str | Path, cell_limit: int = SHEETS_CELL_LIMIT) -> list[TabData]:
    out = []
    for tab in TABS:
        path = Path(out_dir) / tab.csv_name
        if not path.exists():
            raise FileNotFoundError(f"{path} missing - run the crawl/export for the {tab.title} tab first")
        with path.open(encoding="utf-8", newline="") as f:
            reader = csv.reader(f)
            header = next(reader, [])
            td = TabData(tab.title, header)
            for raw in reader:
                row = []
                for col, v in zip(header, raw):
                    val, cut = _fit(_typed(tab, col, v), cell_limit)
                    td.truncated_cells += cut
                    row.append(val)
                td.rows.append(row)
        out.append(td)
    return out


# ---------------------------------------------------------------------------------- xlsx
def write_xlsx(out_dir: str | Path, path: str | Path | None = None) -> dict:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    tabs = load_tabs(out_dir, XLSX_CELL_LIMIT)
    path = Path(path) if path else Path(out_dir) / "frontier_atlas.xlsx"
    wb = Workbook()
    wb.remove(wb.active)
    head_font, head_fill = Font(bold=True), PatternFill("solid", fgColor="E8EEF6")
    for td in tabs:
        ws = wb.create_sheet(td.title)
        ws.append(td.header)
        for c in ws[1]:
            c.font, c.fill = head_font, head_fill
        for r, row in enumerate(td.rows, start=2):
            for c, val in enumerate(row, start=1):
                cell = ws.cell(row=r, column=c, value=val)
                if isinstance(val, str) and val[:1] in "=+-@":
                    cell.data_type = "s"          # text, never a formula
        ws.freeze_panes = "A2"
        if td.header:
            ws.auto_filter.ref = f"A1:{get_column_letter(len(td.header))}{len(td.rows) + 1}"
        for i, col in enumerate(td.header, start=1):
            sample = [len(str(row[i - 1])) for row in td.rows[:200] if i - 1 < len(row) and row[i - 1] is not None]
            ws.column_dimensions[get_column_letter(i)].width = max(10, min(60, max([len(col), *sample]) + 2))
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    summary = {"xlsx": str(path), "tabs": {td.title: len(td.rows) for td in tabs},
               "truncated_cells": sum(td.truncated_cells for td in tabs)}
    log.info("export.xlsx", **{k: v for k, v in summary.items() if k != "tabs"})
    return summary


# ---------------------------------------------------------------------------------- Sheets API
def service_account_token(credentials_file: str | Path) -> Callable[[], str]:
    """Bearer-token provider for a service-account JSON key (refreshed when it expires)."""
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account

    creds = service_account.Credentials.from_service_account_file(str(credentials_file), scopes=SCOPES)

    def token() -> str:
        if not creds.valid:
            creds.refresh(Request())
        return creds.token
    return token


def _a1(title: str) -> str:
    return "'" + title.replace("'", "''") + "'"


def _chunks(rows: list[list], max_bytes: int = 4_000_000, max_rows: int = 5_000):
    """Split rows so each values.update request stays well under the API's payload limit."""
    chunk, size = [], 0
    for row in rows:
        n = len(json.dumps(row, ensure_ascii=False, default=str))
        if chunk and (size + n > max_bytes or len(chunk) >= max_rows):
            yield chunk
            chunk, size = [], 0
        chunk.append(row)
        size += n
    if chunk:
        yield chunk


async def push_to_sheet(
    spreadsheet_id: str,
    out_dir: str | Path,
    http: HttpFetcher,
    token: Callable[[], str],
    *,
    min_interval_s: float = 1.1,          # Sheets write quota: 60 requests / minute / user
) -> dict:
    """Create missing tabs, then clear and rewrite each of the six tabs. Other tabs are untouched."""
    tabs = load_tabs(out_dir, SHEETS_CELL_LIMIT)
    base = f"{SHEETS_API}/{spreadsheet_id}"
    if min_interval_s > 0:
        http.set_host_interval("sheets.googleapis.com", min_interval_s)

    async def call(method: str, url: str, body: dict | None = None, params: dict | None = None) -> dict:
        tok = await asyncio.to_thread(token)
        resp = await http.request(method, url, json=body, params=params,
                                  headers={"Authorization": f"Bearer {tok}"})
        return resp.json() if resp.content else {}

    meta = await call("GET", base, params={"fields": "sheets.properties(sheetId,title,index)"})
    existing = {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta.get("sheets", [])}
    missing = [td.title for td in tabs if td.title not in existing]
    if missing:
        rep = await call("POST", f"{base}:batchUpdate",
                         {"requests": [{"addSheet": {"properties": {"title": t}}} for t in missing]})
        for r in rep.get("replies", []):
            p = r["addSheet"]["properties"]
            existing[p["title"]] = p["sheetId"]

    # size each grid to its data, order the six tabs first, freeze + bold the header
    reqs = []
    for i, td in enumerate(tabs):
        sid = existing[td.title]
        reqs += [
            {"updateSheetProperties": {
                "properties": {"sheetId": sid, "index": i, "gridProperties": {
                    "rowCount": max(2, len(td.rows) + 1), "columnCount": max(1, len(td.header)), "frozenRowCount": 1}},
                "fields": "index,gridProperties(rowCount,columnCount,frozenRowCount)"}},
            {"repeatCell": {"range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1},
                            "cell": {"userEnteredFormat": {"textFormat": {"bold": True},
                                                           "backgroundColor": {"red": .91, "green": .93, "blue": .96}}},
                            "fields": "userEnteredFormat(textFormat,backgroundColor)"}},
        ]
    await call("POST", f"{base}:batchUpdate", {"requests": reqs})

    # a new spreadsheet comes with an empty default tab; drop it ONLY if it really is empty
    removed = []
    for default in ("Sheet1", "Sheet 1"):
        if default in existing and default not in {td.title for td in tabs}:
            probe = await call("GET", f"{base}/values/{quote(_a1(default) + '!A1:Z20')}")
            if not probe.get("values"):
                await call("POST", f"{base}:batchUpdate", {"requests": [{"deleteSheet": {"sheetId": existing[default]}}]})
                removed.append(default)

    written = {}
    for td in tabs:
        await call("POST", f"{base}/values/{quote(_a1(td.title))}:clear", {})
        row0 = 1
        for chunk in _chunks([td.header, *td.rows]):
            rng = f"{_a1(td.title)}!A{row0}"
            await call("PUT", f"{base}/values/{quote(rng)}", {"values": [["" if v is None else v for v in r] for r in chunk]},
                       params={"valueInputOption": "RAW"})          # RAW: text is never parsed as a formula
            row0 += len(chunk)
        await call("POST", f"{base}:batchUpdate", {"requests": [{"setBasicFilter": {"filter": {"range": {
            "sheetId": existing[td.title], "startRowIndex": 0, "endRowIndex": len(td.rows) + 1,
            "startColumnIndex": 0, "endColumnIndex": len(td.header)}}}}]})
        written[td.title] = len(td.rows)
        log.info("export.sheet_tab", tab=td.title, rows=len(td.rows))
    return {"spreadsheet": f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}", "tabs": written,
            "created_tabs": missing, "removed_empty_tabs": removed, "truncated_cells": sum(td.truncated_cells for td in tabs)}
