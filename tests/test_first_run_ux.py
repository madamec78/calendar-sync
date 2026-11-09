from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_web import login, token

from calendar_sync.models import Technician
from calendar_sync.setup import RuntimeConfig, get_setting, load_runtime, save_runtime
from calendar_sync.web import create_app


class Execute:
    def execute(self, **_kwargs):
        return {"items": []}


class CalendarList:
    def list(self, **_kwargs):
        return Execute()


class Calendar:
    def calendarList(self):
        return CalendarList()


class ActivationGoogle:
    def __init__(self, connection_error: Exception | None = None):
        self.connection_error = connection_error
        self.downloads = 0

    def metadata(self, file_id):
        if self.connection_error:
            raise self.connection_error
        return SimpleNamespace(id=file_id, name="plan.xlsx", version="1", md5="", size="1", fingerprint="1::1")

    def calendar(self):
        if self.connection_error:
            raise self.connection_error
        return Calendar()

    def download(self, _file_id, destination: Path):
        self.downloads += 1
        destination.write_bytes(b"mapping fixture")
        return "digest"


def prepared_app(settings, google=None, *, technicians=True):
    settings.oauth_token_file.write_text("mock-token", encoding="utf-8")
    app = create_app(settings, google or ActivationGoogle())
    with app.state.sessions() as db:
        save_runtime(db, RuntimeConfig(drive_file_id="drive-1"))
        if technicians:
            db.add(Technician(workbook_name="Test Technik", email="a@example.test", enabled=True))
        db.commit()
    return app


def activate(client):
    page = client.get("/setup")
    return client.post("/setup/verify-and-activate", data={"csrf_token": token(page)}, follow_redirects=False)


def test_one_click_happy_activation(settings, monkeypatch):
    app = prepared_app(settings)
    monkeypatch.setattr("calendar_sync.web.parse_workbook", lambda *_args, **_kwargs: ([], []))
    monkeypatch.setattr(app.state.service, "sync", lambda *, dry_run: {"status": "ok", "create": 0})
    with TestClient(app) as client:
        login(client)
        response = activate(client)
        assert response.status_code == 303 and response.headers["location"] == "/setup"
        page = client.get("/setup")
        assert "Worker je aktivní" in page.text
        with app.state.sessions() as db:
            assert get_setting(db, "setup_complete") is True
            assert get_setting(db, "next_sync_at")
            assert get_setting(db, "next_backup_at")
            assert app.state.service.settings.delete_enabled is False


def test_activation_blocks_without_enabled_technicians_after_discovery(settings, monkeypatch):
    app = prepared_app(settings, technicians=False)
    monkeypatch.setattr(
        "calendar_sync.web.parse_workbook",
        lambda *_args, **_kwargs: ([], [{"code": "unmapped_technician", "name": "Nova Osoba"}]),
    )
    with TestClient(app) as client:
        login(client)
        response = activate(client)
        assert response.status_code == 400
        assert "Není povolen žádný technik" in response.text
        assert "přiřaďte e-mail nalezenému jménu na stránce Technici" in response.text
        assert response.headers["content-type"].startswith("text/html")
        dashboard = client.get("/", follow_redirects=False)
        assert dashboard.status_code == 200
        assert "Nova Osoba" in client.get("/technicians").text


def test_activation_connection_failure_is_html(settings):
    app = prepared_app(settings, ActivationGoogle(RuntimeError("private detail")))
    with TestClient(app) as client:
        login(client)
        response = activate(client)
        assert response.status_code == 502
        assert "Selhal krok „connection“" in response.text
        assert "Test Drive/Calendar selhal (RuntimeError)" in response.text
        assert "private detail" not in response.text and '"detail"' not in response.text


def test_activation_mapping_fatal_preserves_statuses(settings, monkeypatch):
    app = prepared_app(settings)
    monkeypatch.setattr(
        "calendar_sync.web.parse_workbook",
        lambda *_args, **_kwargs: ([], [{"severity": "fatal", "code": "missing_dates"}]),
    )
    with TestClient(app) as client:
        login(client)
        response = activate(client)
        assert response.status_code == 400
        assert "Selhal krok „mapping“" in response.text and "missing_dates" in response.text
        assert "Zdrojový soubor na Google Drive" in response.text and "hotovo" in response.text


def test_activation_dry_run_failure_is_html(settings, monkeypatch):
    app = prepared_app(settings)
    monkeypatch.setattr("calendar_sync.web.parse_workbook", lambda *_args, **_kwargs: ([], []))
    monkeypatch.setattr(app.state.service, "sync", lambda *, dry_run: {"status": "drift_blocked"})
    with TestClient(app) as client:
        login(client)
        response = activate(client)
        assert response.status_code == 400
        assert "Selhal krok „dry_run“" in response.text and "drift_blocked" in response.text
        with app.state.sessions() as db:
            assert get_setting(db, "setup_complete", False) is False


def test_setup_checklist_exposes_all_states(settings):
    app = create_app(settings, ActivationGoogle())
    with TestClient(app) as client:
        login(client)
        page = client.get("/setup")
        for label in ("Google účet", "Zdrojový soubor na Google Drive", "Načtení techniků z XLSX", "Technici s e-maily", "Kontrolní dry-run", "Aktivace workeru", "Onboarding kalendářů"):
            assert label in page.text
        assert "Ověřit a aktivovat worker" in page.text
        assert "Pokročilé" in client.get("/settings").text


def test_advanced_values_are_preserved(settings):
    app = create_app(settings, ActivationGoogle())
    data = {
        "timezone": "Europe/Prague", "schedule_mode": "times", "interval_minutes": "30",
        "daily_times": "06:15,14:45", "stability_seconds": "90", "backup_enabled": "on",
        "backup_time": "03:10", "backup_retention_count": "21", "backup_retention_days": "45",
        "backup_directory": "archive/db", "drive_file": "https://drive.google.com/file/d/advanced-id/view",
        "calendar_prefix": "ACME – ", "deployment_name": "Advanced", "organization": "ACME",
        "calendar_template": "{organization}: {technician}",
        "sheet_pattern": "Plan-*",
        "technician_name_column": "C", "header_scan_rows": "22", "first_data_column": "F",
        "sync_horizon_days": "180", "auto_detect_dates": "on", "continuation_policy": "strict",
        "stale_policy": "disabled", "assignment_drop_threshold": "0.25",
        "color_map_json": '{"VDF":"7"}', "parse_phones": "on", "parse_addresses": "on",
    }
    with TestClient(app) as client:
        login(client)
        data["csrf_token"] = token(client.get("/settings"))
        response = client.post("/settings/save", data=data, follow_redirects=False)
        assert response.status_code == 303
        with app.state.sessions() as db:
            cfg = load_runtime(db)
            assert (cfg.drive_file_id, cfg.sheet_pattern, cfg.technician_name_column) == ("advanced-id", "Plan-*", "C")
            assert (cfg.header_scan_rows, cfg.first_data_column, cfg.sync_horizon_days) == (22, "F", 180)
            assert cfg.assignment_drop_threshold == pytest.approx(0.25)
            assert cfg.color_map == {"VDF": "7"} and cfg.backup_directory == "archive/db"
            assert cfg.schedule_mode == "times" and cfg.daily_times == ["06:15", "14:45"]
            assert cfg.calendar_template == "{organization}: {technician}"
