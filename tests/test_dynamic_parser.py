from __future__ import annotations

import datetime as dt
import zipfile
from pathlib import Path

from calendar_sync.parser import ParserConfig, TechnicianIdentity, parse_workbook


def workbook(path: Path, rows: list[tuple[int, str, str]], header_row: int = 3, first_col: str = "B", sheet: str = "21.9. - 25.9.2026") -> None:
    serial = (dt.date(2026, 9, 21) - dt.date(1899, 12, 30)).days
    start = ord(first_col) - 65
    cells = [f'<c r="{chr(65 + start + i)}{header_row}"><v>{serial+i}</v></c>' for i in range(5)]
    for row, name, text in rows:
        if name: cells.append(f'<c r="A{row}" t="inlineStr"><is><t>{name}</t></is></c>')
        if text: cells.append(f'<c r="B{row}" t="inlineStr"><is><t>{text}</t></is></c>')
    xml = ''.join(cells)
    sheet_xml = f'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row>{xml}</row></sheetData></worksheet>'
    wb = f'<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="{sheet}" sheetId="1" r:id="rId1"/></sheets></workbook>'
    rels = '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml" Type="x"/></Relationships>'
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('xl/workbook.xml', wb); z.writestr('xl/_rels/workbook.xml.rels', rels); z.writestr('xl/worksheets/sheet1.xml', sheet_xml)


def parse(path: Path):
    techs = [TechnicianIdentity('a', 'Alice', ('A. Tech',)), TechnicianIdentity('b', 'Bob')]
    return parse_workbook(path, 'f', '1', 'm', techs, config=ParserConfig(workbook_profile='org'))


def test_blocks_expand_three_to_four_and_insert_technician(tmp_path):
    p = tmp_path/'a.xlsx'; workbook(p, [(4,'Alice','10001 TMB x'),(5,'','10002 TMB x'),(6,'','10003 TMB x'),(7,'Bob','20001 CTN x'),(8,'','20002 CTN x')])
    events, _ = parse(p); assert [x.technician for x in events].count('Alice') == 3
    workbook(p, [(4,'Alice','10001 TMB x'),(5,'','10002 TMB x'),(6,'','10003 TMB x'),(7,'','10004 TMB x'),(8,'Bob','20001 CTN x')])
    events, _ = parse(p); assert [x.technician for x in events].count('Alice') == 4 and [x.technician for x in events].count('Bob') == 1


def test_shift_and_no_id_move_keep_keys(tmp_path):
    p = tmp_path/'a.xlsx'; workbook(p, [(4,'Alice','visit without id')]); first, _ = parse(p)
    workbook(p, [(10,'Alice','visit without id')]); shifted, _ = parse(p)
    assert first[0].source_key == shifted[0].source_key and 'B4' not in first[0].source_key


def test_unmapped_and_layout_drift_are_reported(tmp_path):
    p = tmp_path/'a.xlsx'; workbook(p, [(4,'Unknown Person','10001 TMB x'),(5,'','10002 TMB x')])
    events, issues = parse(p)
    assert not events and any(x.get('code') == 'unmapped_technician' for x in issues)
    workbook(p, [(4,'Alice','10001 TMB x')], header_row=30)
    _, issues = parse(p)
    assert any(x.get('code') == 'missing_date_headers' and x.get('severity') == 'fatal' for x in issues)


def test_mismatched_embedded_date_is_ignored_with_its_clock_time(tmp_path):
    p = tmp_path/'a.xlsx'
    interval = '22.09.2026 17:00-23:59'
    workbook(p, [(4, 'Alice', f'Příprava na 71120 TMB {interval}'), (5, '', f'71120 TMB {interval}')])
    events, _ = parse(p)
    preparation = next(x for x in events if x.summary.startswith('Příprava'))
    execution = next(x for x in events if not x.summary.startswith('Příprava'))
    assert preparation.start == {'date': '2026-09-21'}
    assert preparation.end == {'date': '2026-09-22'}
    assert execution.start == {'date': '2026-09-21'}
    assert execution.end == {'date': '2026-09-22'}


def test_matching_embedded_date_keeps_clock_time(tmp_path):
    p = tmp_path/'a.xlsx'
    workbook(p, [(4, 'Alice', '71120 TMB 21.09.2026 17:00-23:59')])
    events, _ = parse(p)
    assert events[0].start == {'dateTime': '2026-09-21T17:00:00+02:00', 'timeZone': 'Europe/Prague'}
    assert events[0].end == {'dateTime': '2026-09-21T23:59:00+02:00', 'timeZone': 'Europe/Prague'}
