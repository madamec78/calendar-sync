from __future__ import annotations

import datetime as dt
import zipfile
from pathlib import Path

from calendar_sync.parser import parse_workbook


def make_xlsx(path: Path):
    serials = [(dt.date(2026, 9, 21) - dt.date(1899, 12, 30)).days + i for i in range(5)]
    cells = "".join(f'<c r="{col}3"><v>{value}</v></c>' for col, value in zip("BCDEF", serials))
    # Exact workbook technician label plus a continuation row.
    cells += '<c r="A4" t="inlineStr"><is><t>Test Technik</t></is></c>'
    cells += '<c r="B4" t="inlineStr"><is><t>12345 TMB Praha, Dlouhá 12 +420 777 888 999 21.9.2026 08:00-10:00</t></is></c>'
    cells += '<c r="C5" t="inlineStr"><is><t>54321 CTN pokračování</t></is></c>'
    a4 = cells.find('<c r="A4"'); c5 = cells.find('<c r="C5"')
    sheet = f'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="3">{cells[:a4]}</row><row r="4">{cells[a4:c5]}</row><row r="5">{cells[c5:]}</row></sheetData></worksheet>'
    workbook = '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="21.9. - 25.9.2026" sheetId="1" r:id="rId1"/></sheets></workbook>'
    rels = '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml" Type="x"/></Relationships>'
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/workbook.xml", workbook); z.writestr("xl/_rels/workbook.xml.rels", rels); z.writestr("xl/worksheets/sheet1.xml", sheet)


def test_parser_continuation_full_summary_and_metadata(tmp_path):
    path = tmp_path / "test.xlsx"; make_xlsx(path)
    events, issues = parse_workbook(path, "file-1", "7", "abc", {"Test Technik"})
    assert issues == [] and len(events) == 2
    first = events[0]
    assert first.summary.startswith("12345 TMB")
    assert first.description == "Kontakt: +420 777 888 999"
    assert first.location == "Praha, Dlouhá 12" and first.color_id == "3"
    assert first.start["timeZone"] == "Europe/Prague"
    assert first.private["source_file_id"] == "file-1"
    assert "attendees" not in first.google_body()
    assert events[1].technician == "Test Technik" and events[1].color_id == "9"
