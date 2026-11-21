from __future__ import annotations

import csv
import json
from urllib.parse import unquote

import httpx
import pytest

from frontier_atlas.core.http import HttpFetcher
from frontier_atlas.export import sheets
from frontier_atlas.export.sheets import TABS, TRUNC_MARK, load_tabs, push_to_sheet, write_xlsx

from .conftest import make_settings


def _write(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


@pytest.fixture
def out_dir(tmp_path):
    _write(tmp_path / "startups.csv", ["content.entityName", "content.data.employeeCount", "content.data.foundedYear"],
           [["Acme AI", "12", "2023"], ["=HYPERLINK(\"http://evil\")", "", ""]])
    _write(tmp_path / "products.csv", ["content.productName", "content.pricingModel"], [["Tool", "FREEMIUM"]])
    _write(tmp_path / "research_papers.csv", ["content.title", "content.github_stars"], [["Paper", "1234"]])
    _write(tmp_path / "jobs.csv", ["content.title", "content.is_remote"], [["ML Eng", "True"], ["PM", "False"]])
    _write(tmp_path / "news.csv", ["content.title", "content.word_count", "content.date_confidence", "content.full_text"],
           [["Story", "900", "0.95", "x" * 60_000]])
    _write(tmp_path / "entity_mapping_log.csv", ["raw_name", "canonical_name", "score", "is_seed_entity", "occurrences"],
           [["OpenAI, Inc.", "OpenAI", "100", "True", "3"]])
    return tmp_path


def test_load_tabs_types_and_truncation(out_dir):
    tabs = {t.title: t for t in load_tabs(out_dir)}
    assert [t.title for t in TABS] == list(tabs)            # all six, in the brief's order
    assert tabs["Startups"].rows[0] == ["Acme AI", 12, 2023]
    assert tabs["Startups"].rows[1][1:] == [None, None]
    assert tabs["Research Papers"].rows[0][1] == 1234
    assert [r[1] for r in tabs["Jobs"].rows] == [True, False]
    assert tabs["Entity Mapping Log"].rows[0][2:] == [100.0, True, 3]
    text = tabs["News"].rows[0][3]
    assert len(text) == sheets.SHEETS_CELL_LIMIT and text.endswith(TRUNC_MARK)
    assert tabs["News"].truncated_cells == 1


def test_missing_csv_is_a_clear_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="Startups"):
        load_tabs(tmp_path)


def test_xlsx_six_tabs_formula_safe_and_excel_limit(out_dir):
    from openpyxl import load_workbook

    summary = write_xlsx(out_dir)
    wb = load_workbook(summary["xlsx"])
    assert wb.sheetnames == [t.title for t in TABS]
    ws = wb["Startups"]
    cell = ws["A3"]
    assert cell.data_type == "s" and cell.value.startswith("=HYPERLINK")      # stored as text, not a formula
    assert ws.freeze_panes == "A2" and ws["A1"].font.b
    assert len(wb["News"]["D2"].value) == sheets.XLSX_CELL_LIMIT
    assert summary["tabs"]["Jobs"] == 2 and summary["truncated_cells"] == 1


# ---------------------------------------------------------------- Sheets API (mocked)
class FakeSheetsAPI:
    def __init__(self, existing: dict[str, int], default_has_data: bool = False):
        self.sheets = dict(existing)
        self.default_has_data = default_has_data
        self.log: list[tuple[str, str, dict | None]] = []
        self.values: dict[str, list] = {}
        self.next_id = 100

    def __call__(self, req: httpx.Request) -> httpx.Response:
        assert req.headers["authorization"] == "Bearer test-token"
        path = unquote(req.url.path)
        body = json.loads(req.content) if req.content else None
        self.log.append((req.method, path, body))
        if req.method == "GET" and path.endswith("/sid"):
            return httpx.Response(200, json={"sheets": [{"properties": {"title": t, "sheetId": i}}
                                                         for t, i in self.sheets.items()]})
        if req.method == "GET" and "/values/" in path:
            return httpx.Response(200, json={"values": [["keep me"]]} if self.default_has_data else {})
        if path.endswith(":batchUpdate"):
            replies = []
            for r in body["requests"]:
                if "addSheet" in r:
                    t = r["addSheet"]["properties"]["title"]
                    self.sheets[t] = self.next_id
                    self.next_id += 1
                    replies.append({"addSheet": {"properties": {"title": t, "sheetId": self.sheets[t]}}})
                elif "deleteSheet" in r:
                    self.sheets = {t: i for t, i in self.sheets.items() if i != r["deleteSheet"]["sheetId"]}
            return httpx.Response(200, json={"replies": replies})
        if path.endswith(":clear"):
            return httpx.Response(200, json={})
        if req.method == "PUT":
            assert req.url.params["valueInputOption"] == "RAW"
            self.values.setdefault(path.split("/values/")[1].split("!")[0], []).extend(body["values"])
            return httpx.Response(200, json={})
        return httpx.Response(404)


async def _push(api, out_dir):
    async with HttpFetcher(make_settings(http_max_retries=0),
                           client=httpx.AsyncClient(transport=httpx.MockTransport(api))) as http:
        return await push_to_sheet("sid", out_dir, http, lambda: "test-token", min_interval_s=0)


async def test_push_creates_tabs_writes_raw_and_drops_empty_default(out_dir):
    api = FakeSheetsAPI({"Sheet1": 0, "Startups": 7, "My notes": 9})
    res = await _push(api, out_dir)
    assert res["created_tabs"] == ["Products", "Research Papers", "Jobs", "News", "Entity Mapping Log"]
    assert res["removed_empty_tabs"] == ["Sheet1"]
    assert "My notes" in api.sheets                                         # not ours: untouched
    assert api.values["'Startups'"][0] == ["content.entityName", "content.data.employeeCount", "content.data.foundedYear"]
    assert api.values["'Startups'"][2][0].startswith("=HYPERLINK")         # sent as RAW text
    assert api.values["'Startups'"][2][1] == ""                             # None -> empty cell
    assert api.values["'Jobs'"][1] == ["ML Eng", True]
    cleared = [p for m, p, _ in api.log if p.endswith(":clear")]
    assert len(cleared) == 6
    assert res["tabs"]["Entity Mapping Log"] == 1


async def test_push_keeps_default_tab_with_data(out_dir):
    api = FakeSheetsAPI({"Sheet1": 0}, default_has_data=True)
    res = await _push(api, out_dir)
    assert res["removed_empty_tabs"] == [] and "Sheet1" in api.sheets


def test_chunks_split_large_payloads():
    rows = [["y" * 1000] for _ in range(50)]
    chunks = list(sheets._chunks(rows, max_bytes=10_000))
    assert sum(len(c) for c in chunks) == 50 and all(len(c) <= 10 for c in chunks)
    assert list(sheets._chunks([[1]] * 12, max_rows=5)) == [[[1]] * 5, [[1]] * 5, [[1]] * 2]
