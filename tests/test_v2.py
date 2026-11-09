"""End-to-end and regression tests for the 2.0 behaviour, driven by an in-memory Google stand-in."""
from __future__ import annotations

import datetime as dt

import pytest
from fake_drive_calendar import DEMO_FILE_ID, FakeDriveCalendar
from fastapi.testclient import TestClient
from googleapiclient.errors import HttpError
from httplib2 import Response
from sqlalchemy import select
from test_web import login, token
from xlsx_writer import write_xlsx

from calendar_sync.models import DiscoveredTechnician, OwnedEvent, SyncRun, Technician
from calendar_sync.parser import parse_workbook
from calendar_sync.reconcile import next_stale_state
from calendar_sync.setup import (
    RuntimeConfig,
    calendar_name,
    get_setting,
    load_runtime,
    material_changes,
    save_runtime,
)
from calendar_sync.web import create_app


class Harness:
    def __init__(self, settings):
        self.google = FakeDriveCalendar()
        self.app = create_app(settings, self.google)
        with self.app.state.sessions() as db:
            save_runtime(db, RuntimeConfig(drive_file_id=DEMO_FILE_ID, organization="Demo", interval_minutes=1,
                                           sync_lookback_days=7, stability_seconds=0, backup_frequency="disabled"))
            db.commit()
        self.client = TestClient(self.app)
        self.client.__enter__()
        login(self.client)

    def post(self, url, **data):
        data["csrf_token"] = token(self.client.get("/settings"))
        return self.client.post(url, data=data, follow_redirects=False)

    def run_detail(self, response):
        assert response.status_code == 303, response.text
        return self.client.get(response.headers["location"]).text

    def activate(self):
        assert self.post("/setup/scan").status_code == 303
        with self.app.state.sessions() as db:
            ids = [x.id for x in db.scalars(select(DiscoveredTechnician))]
        assert len(ids) == 3
        for index, item_id in enumerate(ids):
            self.post(f"/discovered/{item_id}/add", email=f"t{index}@example.test")
        assert self.post("/setup/verify-and-activate").headers["location"] == "/setup"
        with self.app.state.sessions() as db:
            assert get_setting(db, "setup_complete") is True
        self.post("/technicians/onboard-all")

    def edit(self, action):
        self.google.simulate_edit(self.google.first_job() if action == "remove" else None)

    def close(self):
        self.client.__exit__(None, None, None)


@pytest.fixture
def sim(settings):
    harness = Harness(settings)
    yield harness
    harness.close()


def test_wizard_activation_sync_and_idempotence(sim):
    sim.activate()
    assert sum(1 for _ in sim.google.calendars) == 3
    first = sim.run_detail(sim.post("/sync", mode="apply"))
    assert "Nové události" in first
    created = sum(len(v) for v in sim.google.calendars.values())
    assert created == 36
    again = sim.run_detail(sim.post("/sync", mode="apply"))
    assert sum(len(v) for v in sim.google.calendars.values()) == created
    with sim.app.state.sessions() as db:
        runs = db.scalars(select(SyncRun).where(SyncRun.mode == "apply").order_by(SyncRun.id)).all()
        assert [r.status for r in runs] == ["ok", "ok"]
    assert "Beze změny" in again


def test_no_attendees_and_foreign_events_are_never_touched(sim):
    sim.activate()
    with sim.app.state.sessions() as db:
        calendar_id = db.scalars(select(Technician)).first().calendar_id
    foreign = sim.google.create_event(calendar_id, {"summary": "Cizí", "start": {"date": "2030-01-01"}, "end": {"date": "2030-01-02"}})
    sim.post("/sync", mode="apply")
    assert foreign["id"] in sim.google._calendar(calendar_id)
    assert sim.google._calendar(calendar_id)[foreign["id"]]["summary"] == "Cizí"
    assert all("attendees" not in e for cal in sim.google.calendars.values() for e in cal.values())


def test_stale_needs_two_distinct_snapshots_and_never_deletes_by_default(sim):
    sim.activate()
    sim.post("/sync", mode="apply")
    total = sum(len(v) for v in sim.google.calendars.values())
    sim.edit("remove")  # snapshot B: one job gone
    sim.post("/sync", mode="apply")
    sim.post("/sync", mode="apply")          # same snapshot again must not count twice
    with sim.app.state.sessions() as db:
        owned = db.scalars(select(OwnedEvent).where(OwnedEvent.missing_streak > 0)).all()
        assert len(owned) == 1 and owned[0].missing_streak == 1 and owned[0].pending_stale is False
    sim.edit("touch")   # snapshot C: confirmed missing
    sim.post("/sync", mode="apply")
    with sim.app.state.sessions() as db:
        owned = db.scalars(select(OwnedEvent).where(OwnedEvent.pending_stale.is_(True))).all()
        assert len(owned) == 1
    assert sum(len(v) for v in sim.google.calendars.values()) == total  # deletion disabled
    assert "ke smazání" in sim.client.get("/stale").text


def test_deletion_requires_confirmation_and_only_removes_pending_owned_events(sim):
    sim.activate()
    data = {"drive_file": "demo-planning-file", "timezone": "Europe/Prague", "organization": "Demo", "calendar_template": "{organization} – {technician}",
            "schedule_mode": "interval", "interval_minutes": "1", "daily_times": "06:00", "stability_seconds": "0", "backup_time": "02:30",
            "backup_retention_count": "14", "backup_retention_days": "30", "backup_directory": "backups", "deployment_name": "Calendar Sync",
            "sheet_pattern": "*", "technician_name_column": "A", "header_scan_rows": "12", "sync_horizon_days": "90", "sync_lookback_days": "0",
            "continuation_policy": "blank-name-continues", "stale_policy": "two-snapshot", "assignment_drop_threshold": "0.5",
            "color_map_json": '{"TMB":"3"}', "auto_detect_dates": "on", "parse_phones": "on", "parse_addresses": "on",
            "calendar_prefix": "Calendar Sync – ", "delete_enabled": "on"}
    refused = sim.post("/settings/save", **data)
    assert refused.status_code == 400 and "potvrzení" in refused.text
    accepted = sim.post("/settings/save", confirm_delete="on", **data)
    assert accepted.status_code == 303 and accepted.headers["location"] == "/setup"  # material change closes the gate
    with sim.app.state.sessions() as db:
        assert get_setting(db, "setup_complete") is False and load_runtime(db).delete_enabled is True


def test_non_material_settings_keep_worker_active(sim):
    sim.activate()
    page = sim.client.get("/settings").text
    assert "Další běh" in page
    with sim.app.state.sessions() as db:
        cfg = load_runtime(db)
    form = {"drive_file": cfg.drive_file_id, "timezone": cfg.timezone, "organization": "Jiná", "calendar_template": cfg.calendar_template,
            "schedule_mode": "interval", "interval_minutes": "5", "daily_times": "06:00", "stability_seconds": "0", "backup_time": "02:30",
            "backup_retention_count": "14", "backup_retention_days": "30", "backup_directory": "backups", "deployment_name": cfg.deployment_name,
            "sheet_pattern": "*", "technician_name_column": "A", "header_scan_rows": "12", "sync_horizon_days": "90", "sync_lookback_days": "7",
            "continuation_policy": "blank-name-continues", "stale_policy": "two-snapshot", "assignment_drop_threshold": "0.5",
            "color_map_json": '{"TMB":"3","CTN":"9","VDF":"11"}', "auto_detect_dates": "on", "parse_phones": "on",
            "parse_addresses": "on", "calendar_prefix": "Calendar Sync – "}
    response = sim.post("/settings/save", **form)
    assert response.status_code == 303 and response.headers["location"] == "/settings"
    with sim.app.state.sessions() as db:
        assert get_setting(db, "setup_complete") is True and get_setting(db, "next_sync_at")
        assert load_runtime(db).interval_minutes == 5


def test_one_failing_technician_does_not_block_the_others(sim, monkeypatch):
    sim.activate()
    with sim.app.state.sessions() as db:
        bad = db.scalars(select(Technician).order_by(Technician.id)).first()
        bad_calendar, bad_name = bad.calendar_id, bad.workbook_name
    original = sim.google.create_event

    def flaky(calendar_id, body):
        if calendar_id == bad_calendar:
            raise HttpError(Response({"status": 500}), b"boom")
        return original(calendar_id, body)

    monkeypatch.setattr(sim.google, "create_event", flaky)
    detail = sim.run_detail(sim.post("/sync", mode="apply"))
    assert "Selhali technici" in detail and bad_name in detail
    assert sum(len(v) for k, v in sim.google.calendars.items() if k != bad_calendar) == 24
    with sim.app.state.sessions() as db:
        assert db.scalars(select(SyncRun).order_by(SyncRun.id.desc())).first().status == "partial"
        assert db.get(Technician, bad.id).last_error == "Google API HTTP 500"


def test_update_of_event_deleted_by_user_recreates_it(sim):
    sim.activate()
    sim.post("/sync", mode="apply")
    calendar_id = next(iter(sim.google.calendars))
    events = sim.google._calendar(calendar_id)
    victim_id, victim = next(iter(events.items()))
    victim["summary"] = "Ručně změněno"  # forces an update
    original = sim.google.update_event
    calls = []

    def gone(cal, event_id, body):
        calls.append(event_id)
        sim.google._calendar(cal).pop(event_id, None)
        return original(cal, event_id, body)  # raises 404 because the event is gone

    sim.google.update_event = gone
    sim.post("/sync", mode="apply")
    assert victim_id in calls
    assert any(e["summary"].startswith("10") or e["summary"] for e in sim.google._calendar(calendar_id).values())
    assert len(sim.google._calendar(calendar_id)) == 12
    with sim.app.state.sessions() as db:
        assert db.scalars(select(SyncRun).order_by(SyncRun.id.desc())).first().status == "ok"


def test_dry_run_writes_nothing(sim):
    sim.activate()
    before = {k: dict(v) for k, v in sim.google.calendars.items()}
    detail = sim.run_detail(sim.post("/sync", mode="dry-run"))
    assert "Dry-run: do Google kalendářů se nic nezapsalo" in detail
    assert {k: dict(v) for k, v in sim.google.calendars.items()} == before


def test_onboard_all_reports_invitations_once(sim):
    sim.activate()
    with sim.app.state.sessions() as db:
        assert all(t.calendar_id for t in db.scalars(select(Technician)))
    response = sim.post("/technicians/onboard-all")  # nothing left to onboard
    assert response.status_code == 303
    assert "0 hotovo" in sim.client.get("/technicians").text


def test_stale_state_machine_is_idempotent_for_same_snapshot():
    assert next_stale_state(1, True, already_counted=True) == (1, False)
    assert next_stale_state(1, True, already_counted=False) == (2, True)
    assert next_stale_state(5, False) == (0, False)


def test_calendar_name_template_is_authoritative():
    cfg = RuntimeConfig(organization="ACME", calendar_template="{organization}: {technician}", calendar_prefix="IGNORED ")
    assert calendar_name(cfg, "Jan") == "ACME: Jan"
    cfg.calendar_template = "{broken"
    assert calendar_name(cfg, "Jan") == "IGNORED Jan"


def test_material_changes_only_list_parsing_fields():
    before = RuntimeConfig(drive_file_id="a")
    after = RuntimeConfig(drive_file_id="a", interval_minutes=9, organization="X", backup_time="01:00")
    assert material_changes(before, after) == []
    assert material_changes(before, RuntimeConfig(drive_file_id="b", delete_enabled=True)) == ["drive_file_id", "delete_enabled"]


def test_xlsx_writer_round_trips_through_parser(tmp_path):
    path = tmp_path / "w.xlsx"
    day = dt.date(2026, 11, 9)
    serial = (day - dt.date(1899, 12, 30)).days
    write_xlsx(path, {"9.11. - 13.11.2026": {**{f"{c}3": serial + i for i, c in enumerate("BCDEF")},
                                              "A4": "Žofie Šťastná", "B4": "12345 TMB Praha, Dlouhá 12 <&> 9.11.2026 08:00-10:00"}})
    events, issues = parse_workbook(path, "f", "1", "m", {"Žofie Šťastná"}, date_from=day, date_through=day)
    assert issues == [] and len(events) == 1 and events[0].summary.endswith("<&> 9.11.2026 08:00-10:00")
    assert events[0].start["dateTime"].startswith("2026-11-09T08:00")


def test_fake_backend_is_self_consistent(settings):
    google = FakeDriveCalendar()
    assert google.has_token() and google.credentials() is not None
    meta = google.metadata("x")
    assert meta.md5 == google.download("x", settings.data_dir / "x.xlsx")
    version = google.simulate_edit("10000")
    assert google.metadata("x").version == version


def test_xlsx_edit_does_not_rewrite_unaffected_events(sim):
    sim.activate()
    sim.post("/sync", mode="apply")
    sim.edit("touch")  # new Drive version/hash, identical content
    detail = sim.run_detail(sim.post("/sync", mode="apply"))
    with sim.app.state.sessions() as db:
        summary = __import__("json").loads(db.scalars(select(SyncRun).order_by(SyncRun.id.desc())).first().summary_json)
    assert summary["update"] == 0 and summary["create"] == 0 and summary["unchanged"] == 36, detail


def test_cli_sync_uses_runtime_settings_from_database(settings, monkeypatch, capsys):
    import sys

    from calendar_sync import cli
    from calendar_sync.models import init_db, make_session_factory
    from calendar_sync.setup import save_runtime
    engine, sessions = make_session_factory(settings.database_url)
    init_db(engine)
    with sessions() as db:
        save_runtime(db, RuntimeConfig(drive_file_id="db-file-id", timezone="Europe/Prague"))
        db.commit()
    seen = {}

    class FakeService:
        def __init__(self, cfg, *_a): seen["file"] = cfg.source_file_id; seen["delete"] = cfg.delete_enabled
        def sync(self, dry_run): return {"status": "ok", "dry": dry_run}

    monkeypatch.setattr(cli, "SyncService", FakeService)
    monkeypatch.setattr(cli.Settings, "from_env", classmethod(lambda cls: settings))
    monkeypatch.setattr(cli, "GoogleClient", lambda *a: object())
    monkeypatch.setattr(sys, "argv", ["calendar-sync", "dry-run"])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 0 and seen == {"file": "db-file-id", "delete": False}
