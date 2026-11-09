"""Minimal XLSX writer (stdlib only) used by tests.

It produces just enough of the SpreadsheetML package for ``parser.xlsx_cells`` and for real
spreadsheet applications to open it: inline strings and numbers, several sheets.
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

from calendar_sync.parser import CELL_RE, col_number

_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _sheet_xml(cells: dict[str, object]) -> str:
    rows: dict[int, list[tuple[int, str, object]]] = {}
    for ref, value in cells.items():
        match = CELL_RE.match(ref)
        if match is None:
            raise ValueError(f"invalid cell reference {ref!r}")
        rows.setdefault(int(match.group(2)), []).append((col_number(match.group(1)), ref, value))
    out = []
    for number in sorted(rows):
        parts = []
        for _, ref, value in sorted(rows[number]):
            if isinstance(value, (int, float)):
                parts.append(f'<c r="{ref}"><v>{value}</v></c>')
            else:
                text = escape(_ILLEGAL.sub("", str(value)))
                parts.append(f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>')
        out.append(f'<row r="{number}">{"".join(parts)}</row>')
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f'<sheetData>{"".join(out)}</sheetData></worksheet>')


def write_xlsx(path: Path, sheets: dict[str, dict[str, object]]) -> None:
    """Write ``{sheet name: {cell ref: value}}`` to ``path``."""
    names = list(sheets)
    workbook = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
                + "".join(f'<sheet name="{escape(n, {chr(34): "&quot;"})}" sheetId="{i}" r:id="rId{i}"/>'
                          for i, n in enumerate(names, 1))
                + "</sheets></workbook>")
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                      f'Target="worksheets/sheet{i}.xml"/>' for i in range(1, len(names) + 1))
            + "</Relationships>")
    content_types = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                     '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                     '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                     '<Default Extension="xml" ContentType="application/xml"/>'
                     '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                     + "".join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                               for i in range(1, len(names) + 1))
                     + "</Types>")
    root_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                 '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                 '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
                 'Target="xl/workbook.xml"/></Relationships>')
    path.parent.mkdir(parents=True, exist_ok=True)
    def put(archive: zipfile.ZipFile, name: str, data: str) -> None:
        info = zipfile.ZipInfo(name, date_time=(2000, 1, 1, 0, 0, 0))  # fixed timestamp: reproducible bytes
        info.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(info, data)

    with zipfile.ZipFile(path, "w") as archive:
        put(archive, "[Content_Types].xml", content_types)
        put(archive, "_rels/.rels", root_rels)
        put(archive, "xl/workbook.xml", workbook)
        put(archive, "xl/_rels/workbook.xml.rels", rels)
        for i, name in enumerate(names, 1):
            put(archive, f"xl/worksheets/sheet{i}.xml", _sheet_xml(sheets[name]))
