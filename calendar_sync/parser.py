from __future__ import annotations

import datetime as dt
import fnmatch
import hashlib
import json
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Prague")
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main", "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
RELNS = {"p": "http://schemas.openxmlformats.org/package/2006/relationships"}
SHEET_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})\.\s*-\s*(\d{1,2})\.(\d{1,2})\.(\d{4})$")
CELL_RE = re.compile(r"^([A-Z]+)(\d+)$")
JOB_RE = re.compile(r"(?<!\d)(\d{5})(?!\d)")
INTERVAL_RE = re.compile(r"(?<!\d)(\d{1,2})\.(\d{1,2})\.(\d{4})\s+(\d{1,2}):(\d{2})\s*[-–]\s*(?:(\d{1,2})\.(\d{1,2})\.(\d{4})\s+)?(\d{1,2}):(\d{2})(?!\d)")
CATEGORY_RE = re.compile(r"^\s*\d{5}[\s_:\-]*(\w+)\b", re.IGNORECASE)
DEFAULT_COLORS = {"TMB": "3", "CTN": "9", "VDF": "11"}
PHONE_RE = re.compile(r"(?<![\w\d])(?:\+420|00420)(?:[ .-]*\d){9}(?![\w\d])|(?<![\w\d])(?:[2-9]\d{2}(?:[ .-]?\d{3}){2})(?![\w\d])")
ADDRESS_RE = re.compile(r"\b(Praha(?:\s+[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ][A-Za-zÁ-ž-]+)?\s*,\s*(?:U\s+[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ][A-Za-zÁ-ž -]+|[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ][A-Za-zÁ-ž -]+\s+\d+(?:/\d+)?))(?=\b|_)")
STREET_CITY_RE = re.compile(r"\b([A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ][A-Za-zÁ-ž-]+(?:\s+[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ][A-Za-zÁ-ž-]+)*\s+\d+(?:/\d+)?\s*,\s*[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ][A-Za-zÁ-ž-]+(?:\s+\d{1,2})?)(?=\b|[,.])")


def col_number(value: str | int) -> int:
    if isinstance(value, int): return value
    out = 0
    for char in value.strip().upper(): out = out * 26 + ord(char) - 64
    if out < 1: raise ValueError("invalid column")
    return out


def col_name(value: int) -> str:
    out = ""
    while value: value, rem = divmod(value - 1, 26); out = chr(65 + rem) + out
    return out


@dataclass(frozen=True)
class TechnicianIdentity:
    canonical_id: str
    name: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class ParserConfig:
    workbook_profile: str = "default"
    sheet_pattern: str = "*"
    technician_name_column: str = "A"
    header_scan_rows: int = 12
    first_data_column: str | None = None
    auto_detect_dates: bool = True
    continuation_policy: str = "blank-name-continues"
    color_map: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_COLORS))
    parse_phones: bool = True
    parse_addresses: bool = True


@dataclass(frozen=True)
class Event:
    technician: str
    source_key: str
    summary: str
    description: str
    location: str | None
    color_id: str | None
    start: dict[str, str]
    end: dict[str, str]
    private: dict[str, str]

    def google_body(self) -> dict:
        body = {"summary": self.summary, "description": self.description, "start": self.start, "end": self.end, "extendedProperties": {"private": self.private}}
        if self.location: body["location"] = self.location
        if self.color_id: body["colorId"] = self.color_id
        return body


def norm(value: object) -> str: return re.sub(r"\s+", " ", str(value or "")).strip()
def cell_text(value: object) -> str: return str(value or "").replace("\r\n", "\n").replace("\r", "\n")


def parse_sheet_name(name: str):
    match = SHEET_RE.match(name)
    if not match: return None
    d1, m1, d2, m2, year = map(int, match.groups()); end = dt.date(year, m2, d2)
    start = dt.date(year - 1 if m1 == 12 and m2 == 1 else year, m1, d1)
    return (start, end) if end >= start else None


def excel_date(value: object) -> dt.date:
    if isinstance(value, (dt.date, dt.datetime)): return value.date() if isinstance(value, dt.datetime) else value
    return (dt.datetime(1899, 12, 30, tzinfo=dt.UTC) + dt.timedelta(days=float(str(value)))).date()


def xlsx_cells(path: Path):
    with zipfile.ZipFile(path) as archive:
        workbook = ET.fromstring(archive.read("xl/workbook.xml")); rel = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        targets = {x.attrib["Id"]: x.attrib["Target"] for x in rel.findall("p:Relationship", RELNS)}; shared = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ET.fromstring(archive.read("xl/sharedStrings.xml")); shared = ["".join(t.text or "" for t in item.iter(f"{{{NS['m']}}}t")) for item in root.findall("m:si", NS)]
        sheets = workbook.find("m:sheets", NS)
        for sheet in (sheets if sheets is not None else []):
            name = sheet.attrib["name"]; target = targets[sheet.attrib[f"{{{NS['r']}}}id"]].lstrip("/")
            if not target.startswith("xl/"): target = "xl/" + target
            root = ET.fromstring(archive.read(target)); cells = {}
            for cell in root.findall(".//m:c", NS):
                ref = cell.attrib.get("r"); typ = cell.attrib.get("t"); value = cell.find("m:v", NS)
                if not ref: continue
                parsed: object
                if typ == "inlineStr": parsed = "".join(t.text or "" for t in cell.iter(f"{{{NS['m']}}}t"))
                elif value is None: parsed = ""
                elif typ == "s": parsed = shared[int(value.text or "0")]
                else:
                    try: parsed = float(value.text or "")
                    except ValueError: parsed = value.text or ""
                cells[ref] = parsed
            yield name, cells


def parse_interval(text: str, zone: ZoneInfo = TZ):
    matches = []
    for m in INTERVAL_RE.finditer(text):
        d, mo, y, hh, mm, d2, mo2, y2, hh2, mm2 = m.groups(); start = dt.datetime(int(y), int(mo), int(d), int(hh), int(mm), tzinfo=zone)
        end = dt.datetime(int(y2 or y), int(mo2 or mo), int(d2 or d), int(hh2), int(mm2), tzinfo=zone)
        if end <= start and not d2: end += dt.timedelta(days=1)
        if end > start: matches.append((start, end))
    return matches[-1] if matches else None


def phones(text: str) -> list[str]: return list(dict.fromkeys(m.group(0).strip() for m in PHONE_RE.finditer(text)))
def assignment_interval(text: str, assigned_date: dt.date, zone: ZoneInfo = TZ):
    """Use an embedded interval only when its date agrees with the spreadsheet column."""
    parsed = parse_interval(text, zone)
    if parsed is None:
        return None
    source_start, source_end = parsed
    if source_start.date() != assigned_date:
        return None
    start = dt.datetime.combine(assigned_date, source_start.timetz())
    end_date = assigned_date + (source_end.date() - source_start.date())
    end = dt.datetime.combine(end_date, source_end.timetz())
    if end <= start:
        end += dt.timedelta(days=1)
    return start, end


def location(text: str) -> str | None:
    found: list[str] = []
    for pattern in (ADDRESS_RE, STREET_CITY_RE): found.extend(m.group(1).strip(" ,._") for m in pattern.finditer(text) if "_" not in m.group(1))
    found = list(dict.fromkeys(found)); return found[0] if len(found) == 1 else None


def technician_index(technicians) -> dict[str, TechnicianIdentity]:
    result = {}
    if isinstance(technicians, set): identities = [TechnicianIdentity(hashlib.sha256(x.casefold().encode()).hexdigest()[:16], x) for x in technicians]
    elif isinstance(technicians, dict):
        identities = []
        for key, value in technicians.items():
            if isinstance(value, TechnicianIdentity): identities.append(value)
            elif isinstance(value, dict): identities.append(TechnicianIdentity(str(value.get("canonical_id", key)), str(value.get("name", key)), tuple(value.get("aliases", ()))))
            else: identities.append(TechnicianIdentity(str(key), str(value)))
    else: identities = list(technicians)
    for item in identities:
        for label in (item.name, *item.aliases): result[norm(label).casefold()] = item
    return result


def detect_date_columns(cells: dict, config: ParserConfig) -> tuple[dict[int, dt.date], int | None]:
    start_col = col_number(config.first_data_column) if config.first_data_column else 1
    max_col = max((col_number(m.group(1)) for ref in cells if (m := CELL_RE.match(ref))), default=start_col)
    best: tuple[dict[int, dt.date], int | None] = ({}, None)
    for row in range(1, config.header_scan_rows + 1):
        found = {}
        for col in range(start_col, max_col + 1):
            try: day = excel_date(cells.get(f"{col_name(col)}{row}"))
            except (TypeError, ValueError, OverflowError): continue
            if 2000 <= day.year <= 2100: found[col] = day
        if len(found) > len(best[0]): best = found, row
    return best


def layout_fingerprint(sheets: list[dict]) -> str:
    raw = json.dumps(sheets, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def parse_workbook(path: Path, file_id: str, version: str, md5: str, technicians, timezone_name: str = "Europe/Prague", config: ParserConfig | None = None, date_from: dt.date | None = None, date_through: dt.date | None = None) -> tuple[list[Event], list[dict]]:
    report_layout = config is not None
    config = config or ParserConfig(); zone = ZoneInfo(timezone_name); identities = technician_index(technicians)
    atoms: list[dict] = []; issues: list[dict] = []; layouts = []; matched_sheets = 0; assignment_cells = 0
    for sheet, cells in xlsx_cells(path):
        if not fnmatch.fnmatchcase(sheet, config.sheet_pattern): continue
        span = parse_sheet_name(sheet)
        if not span: continue
        if date_from is not None and span[1] < date_from: continue
        if date_through is not None and span[0] > date_through: continue
        matched_sheets += 1; dates, header_row = detect_date_columns(cells, config)
        dates = {col: day for col, day in dates.items() if (date_from is None or day >= date_from) and (date_through is None or day <= date_through)}
        expected = {span[0] + dt.timedelta(days=i) for i in range((span[1] - span[0]).days + 1)}
        expected = {day for day in expected if (date_from is None or day >= date_from) and (date_through is None or day <= date_through)}
        layouts.append({"sheet": sheet, "header_row": header_row, "date_columns": sorted((k, v.isoformat()) for k, v in dates.items()), "name_column": config.technician_name_column})
        if not dates or not expected.issubset(set(dates.values())):
            issues.append({"severity": "fatal", "code": "missing_date_headers", "sheet": sheet, "reason": "date headers missing or do not match sheet name"}); continue
        current: TechnicianIdentity | None = None; current_label = ""; name_col = config.technician_name_column.upper()
        rows = sorted({int(m.group(2)) for ref in cells if (m := CELL_RE.match(ref)) and int(m.group(2)) > (header_row or 0)})
        for row in rows:
            label = norm(cells.get(f"{name_col}{row}", ""))
            if label:
                current_label = label; current = identities.get(label.casefold())
                if current is None: issues.append({"severity": "warning", "code": "unmapped_technician", "sheet": sheet, "row": row, "name": label, "reason": "unmapped technician"})
            elif config.continuation_policy != "blank-name-continues": current = None
            if current is None: continue
            for col, day in dates.items():
                text = cell_text(cells.get(f"{col_name(col)}{row}", ""))
                if not text.strip(): continue
                assignment_cells += 1; job_match = JOB_RE.search(text); job = job_match.group(1) if job_match else None
                normalized = norm(text).casefold(); content_hash = hashlib.sha256(normalized.encode()).hexdigest()[:16]
                atoms.append({"technician": current.name, "canonical_id": current.canonical_id, "sheet": sheet, "date": day, "text": text, "job": job, "content_hash": content_hash, "interval": assignment_interval(text, day, zone), "label": current_label})
    if matched_sheets == 0: issues.append({"severity": "fatal", "code": "sheet_pattern_changed", "reason": "no horizon-relevant sheet matched"})
    deduped: list[dict] = []
    seen_issues: set[tuple] = set()
    for issue in issues:
        key = (issue.get("code"), norm(issue.get("name")).casefold(), issue.get("sheet")) if issue.get("code") == "unmapped_technician" else (issue.get("code"), issue.get("sheet"), issue.get("reason"))
        if key not in seen_issues:
            seen_issues.add(key); deduped.append(issue)
    issues = deduped
    counts: dict[tuple, int] = {}
    for atom in atoms:
        base = (atom["canonical_id"], atom["date"], atom["job"] or f"noid:{atom['content_hash']}", atom["content_hash"])
        occurrence = counts.get(base, 0); counts[base] = occurrence + 1
        token = atom["job"] or f"noid:{atom['content_hash']}"
        atom["source_key"] = f"v2|{file_id}|{config.workbook_profile}|{atom['canonical_id']}|{atom['date'].isoformat()}|{token}|{atom['content_hash']}|{occurrence}"
    grouped: dict[tuple, list[dict]] = {}; output = []
    for atom in atoms:
        if atom["job"] and atom["interval"]: grouped.setdefault((atom["canonical_id"], atom["job"], atom["content_hash"], atom["interval"]), []).append(atom)
        else: output.append(atom)
    for group in grouped.values():
        start, end = group[0]["interval"]; overlap = [x for x in group if start.date() <= x["date"] <= end.date()]
        if len(overlap) > 1: output.append(min(overlap, key=lambda x: x["date"] != start.date())); output.extend(x for x in group if x not in overlap)
        else: output.extend(group)
    events = []
    for atom in sorted(output, key=lambda x: x["source_key"]):
        found_phones = phones(atom["text"]) if config.parse_phones else []; interval = atom["interval"]
        if interval: start = {"dateTime": interval[0].isoformat(), "timeZone": timezone_name}; end = {"dateTime": interval[1].isoformat(), "timeZone": timezone_name}
        else: start = {"date": atom["date"].isoformat()}; end = {"date": (atom["date"] + dt.timedelta(days=1)).isoformat()}
        category = CATEGORY_RE.search(atom["text"]); private = {"sync_mark": "calendar_planning_v2", "source_file_id": file_id, "workbook_profile": config.workbook_profile, "source_key": atom["source_key"], "canonical_technician_id": atom["canonical_id"], "technician_name": atom["technician"], "source_sheet": atom["sheet"], "source_version": version, "source_hash": md5}
        events.append(Event(atom["technician"], atom["source_key"], atom["text"], "Kontakt: " + ", ".join(found_phones) if found_phones else "", location(atom["text"]) if config.parse_addresses else None, config.color_map.get(category.group(1).upper()) if category else None, start, end, private))
    if report_layout:
        issues.append({"severity": "info", "code": "layout", "layout_fingerprint": layout_fingerprint(layouts), "assignments": assignment_cells, "matched_sheets": matched_sheets})
    return events, issues
