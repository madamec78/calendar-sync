from __future__ import annotations

import datetime as dt

from calendar_sync.parser import ParserConfig, TechnicianIdentity, detect_pairs, parse_workbook
from tests.xlsx_writer import write_xlsx

VESELY = TechnicianIdentity("vesely", "Veselý Jiří")
PAZICAN = TechnicianIdentity("pazican", "Pažičan Petr")
GYGAL = TechnicianIdentity("gygal", "Gygal Radek")
MRAZEK = TechnicianIdentity("mrazek", "Mrázek Martin")
SOLO = TechnicianIdentity("solo", "Solo")
ALL = [VESELY, PAZICAN, GYGAL, MRAZEK, SOLO]


def sheet(rows: dict[str, str], start: dt.date = dt.date(2026, 9, 21)) -> dict[str, object]:
    serial = (start - dt.date(1899, 12, 30)).days
    cells: dict[str, object] = {f"{chr(66 + i)}3": serial + i for i in range(5)}
    cells.update(rows)
    return cells


def parse(tmp_path, rows, extra_sheets=None):
    path = tmp_path / "plan.xlsx"
    sheets = {"21.9. - 25.9.2026": sheet(rows), **(extra_sheets or {})}
    write_xlsx(path, sheets)
    return parse_workbook(path, "f", "1", "m", ALL, config=ParserConfig(workbook_profile="org"))


def names(events):
    return sorted((e.technician, e.summary) for e in events)


def test_names_in_consecutive_rows_form_a_pair_and_share_everything(tmp_path):
    events, issues = parse(tmp_path, {"A4": "Veselý Jiří", "B4": "10001 TMB Praha, Dlouhá 1", "C4": "volno", "A5": "Pažičan Petr", "B5": "10002 TMB b"})
    assert names(events) == [
        ("Pažičan Petr", "10001 TMB Praha, Dlouhá 1"), ("Pažičan Petr", "10002 TMB b"), ("Pažičan Petr", "volno"),
        ("Veselý Jiří", "10001 TMB Praha, Dlouhá 1"), ("Veselý Jiří", "10002 TMB b"), ("Veselý Jiří", "volno"),
    ]
    copy = next(e for e in events if e.technician == "Pažičan Petr" and e.summary.startswith("10001"))
    assert copy.private["canonical_technician_id"] == "pazican" and copy.private["technician_name"] == "Pažičan Petr"
    assert "|pazican|" in copy.source_key and copy.source_key == copy.private["source_key"]
    assert {"severity": "info", "code": "pair", "sheet": "21.9. - 25.9.2026", "reason": "Veselý Jiří + Pažičan Petr"} in issues


def test_job_in_continuation_row_is_shared(tmp_path):
    # Zakázka v třetím řádku bloku (prázdné jméno) patří poslednímu jménu a sdílí se s partnerem.
    events, _ = parse(tmp_path, {"A4": "Veselý Jiří", "A5": "Pažičan Petr", "B6": "72160 CTN KOHVL"})
    assert names(events) == [("Pažičan Petr", "72160 CTN KOHVL"), ("Veselý Jiří", "72160 CTN KOHVL")]


def test_job_listed_for_both_is_not_duplicated(tmp_path):
    events, _ = parse(tmp_path, {"A4": "Veselý Jiří", "B4": "10001 TMB x", "A5": "Pažičan Petr", "B5": "10001 TMB x"})
    assert len(events) == 2 and names(events) == [("Pažičan Petr", "10001 TMB x"), ("Veselý Jiří", "10001 TMB x")]


def test_names_with_a_gap_are_not_a_pair(tmp_path):
    events, issues = parse(tmp_path, {"A4": "Veselý Jiří", "B4": "10001 TMB a", "A6": "Pažičan Petr", "B6": "10002 TMB b"})
    assert names(events) == [("Pažičan Petr", "10002 TMB b"), ("Veselý Jiří", "10001 TMB a")]
    assert not any(i["code"] == "pair" for i in issues)


def test_two_pairs_do_not_mix_and_solo_untouched(tmp_path):
    events, _ = parse(tmp_path, {"A4": "Veselý Jiří", "B4": "10001 TMB a", "A5": "Pažičan Petr",
                                 "A8": "Gygal Radek", "B8": "20001 TMB b", "A9": "Mrázek Martin", "C9": "Martin není",
                                 "A12": "Solo", "B12": "30001 TMB c"})
    assert names(events) == [
        ("Gygal Radek", "20001 TMB b"), ("Gygal Radek", "Martin není"),
        ("Mrázek Martin", "20001 TMB b"), ("Mrázek Martin", "Martin není"),
        ("Pažičan Petr", "10001 TMB a"),
        ("Solo", "30001 TMB c"),
        ("Veselý Jiří", "10001 TMB a"),
    ]


def test_pairs_are_detected_per_sheet(tmp_path):
    other = {"28.9. - 2.10.2026": sheet({"A4": "Veselý Jiří", "B4": "10003 TMB week2", "A6": "Pažičan Petr"}, dt.date(2026, 9, 28))}
    events, _ = parse(tmp_path, {"A4": "Veselý Jiří", "B4": "10001 TMB week1", "A5": "Pažičan Petr"}, other)
    assert names(events) == [("Pažičan Petr", "10001 TMB week1"), ("Veselý Jiří", "10001 TMB week1"), ("Veselý Jiří", "10003 TMB week2")]


def test_identical_repeated_jobs_keep_their_count(tmp_path):
    events, _ = parse(tmp_path, {"A4": "Pažičan Petr", "A5": "Veselý Jiří", "B5": "10001 visit", "B6": "10001 visit"})
    assert [e.technician for e in events].count("Veselý Jiří") == 2
    assert [e.technician for e in events].count("Pažičan Petr") == 2
    assert len({e.source_key for e in events}) == 4


def test_unmanaged_neighbour_does_not_form_a_pair():
    assert detect_pairs({4: VESELY}) == [] and detect_pairs({4: VESELY, 5: VESELY}) == []
    assert [[m.canonical_id for m in g] for g in detect_pairs({4: VESELY, 5: PAZICAN, 9: GYGAL})] == [["vesely", "pazican"]]
