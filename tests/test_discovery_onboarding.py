from __future__ import annotations

import pytest
from conftest import FakeGoogle
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select, text
from test_web import login, token

from calendar_sync.discovery import record_discovered
from calendar_sync.google import GoogleClient
from calendar_sync.models import DiscoveredTechnician, OwnedEvent, SyncRun, Technician, init_db
from calendar_sync.setup import set_setting
from calendar_sync.web import create_app


def activate(app):
    with app.state.sessions() as db:
        set_setting(db, "setup_complete", True)
        db.commit()


def test_discovery_persists_deduplicates_filters_and_survives_session(settings):
    app = create_app(settings, FakeGoogle())
    issues = [
        {"code": "unmapped_technician", "name": "  Žák   Čeněk "},
        {"code": "unmapped_technician", "name": "žák čeněk"},
        {"code": "unmapped_technician", "name": "Legenda: barvy"},
        {"code": "unmapped_technician", "name": "Technik"},
        {"code": "unmapped_technician", "name": ""},
    ]
    with app.state.sessions() as db:
        record_discovered(db, issues)
    with app.state.sessions() as db:
        items = list(db.scalars(select(DiscoveredTechnician)))
        assert len(items) == 1
        assert items[0].display_name == "Žák Čeněk"


def test_init_db_adds_discovery_table_to_existing_database(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE technicians (id INTEGER PRIMARY KEY, workbook_name VARCHAR(250) UNIQUE NOT NULL)"))
        connection.execute(text("INSERT INTO technicians (workbook_name) VALUES ('Existing')"))
    init_db(engine)
    assert "discovered_technicians" in inspect(engine).get_table_names()
    with engine.connect() as connection:
        assert connection.execute(text("SELECT workbook_name FROM technicians")).scalar_one() == "Existing"


def test_init_db_repairs_null_owned_event_state(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'old-owned.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE technicians (id INTEGER PRIMARY KEY, workbook_name VARCHAR(250) UNIQUE NOT NULL)"))
        connection.execute(text("INSERT INTO technicians (id, workbook_name) VALUES (1, 'Alice')"))
        connection.execute(text("CREATE TABLE owned_events (id INTEGER PRIMARY KEY, technician_id INTEGER NOT NULL, source_key VARCHAR(700) NOT NULL, google_event_id VARCHAR(300) NOT NULL, missing_streak INTEGER, pending_stale BOOLEAN, last_seen_fingerprint VARCHAR(300), UNIQUE (technician_id, source_key))"))
        connection.execute(text("INSERT INTO owned_events (technician_id, source_key, google_event_id, missing_streak, pending_stale) VALUES (1, 'key', 'event', NULL, NULL)"))
    init_db(engine)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT missing_streak, pending_stale FROM owned_events")).one() == (0, 0)


def test_candidate_add_ignore_restore_inline_update_and_guarded_remove(settings):
    app = create_app(settings, FakeGoogle()); activate(app)
    with app.state.sessions() as db:
        record_discovered(db, [{"code": "unmapped_technician", "name": " Nova  Osoba "}])
        item_id = db.scalar(select(DiscoveredTechnician)).id
    with TestClient(app) as client:
        login(client)
        home = client.get("/technicians")
        assert "Nalezení v XLSX" in home.text and "Nova Osoba" in home.text
        csrf = token(home)
        assert client.post(f"/discovered/{item_id}/ignore", data={"csrf_token": csrf}).status_code == 200
        home = client.get("/technicians")
        assert "Ignorovaná jména (1)" in home.text
        csrf = token(home)
        client.post(f"/discovered/{item_id}/restore", data={"csrf_token": csrf})
        home = client.get("/technicians"); csrf = token(home)
        client.post(f"/discovered/{item_id}/add", data={"email": "JIRI@EXAMPLE.CZ", "csrf_token": csrf})
        with app.state.sessions() as db:
            tech = db.scalar(select(Technician)); tech_id = tech.id
            assert tech.email == "jiri@example.cz"
            tech.calendar_id = "owned-calendar"; db.add(OwnedEvent(technician_id=tech.id, source_key="k", google_event_id="e")); db.commit()
        home = client.get("/technicians")
        assert "Nova Osoba" in home.text and home.text.count("Nova Osoba") >= 1
        csrf = token(home)
        client.post(f"/technicians/{tech_id}/update", data={"email": "new@example.cz", "csrf_token": csrf})
        with app.state.sessions() as db:
            assert db.get(Technician, tech_id).enabled is False
        csrf = token(client.get("/technicians"))
        assert client.post(f"/technicians/{tech_id}/remove", data={"csrf_token": csrf}).status_code == 409
        csrf = token(client.get("/technicians"))
        assert client.post(f"/technicians/{tech_id}/remove", data={"confirm": "yes", "csrf_token": csrf}).status_code == 200
        with app.state.sessions() as db:
            assert db.get(Technician, tech_id) is None


def test_discovered_names_make_dashboard_available_during_first_run(settings):
    app = create_app(settings, FakeGoogle())
    with app.state.sessions() as db:
        record_discovered(db, [{"code": "unmapped_technician", "name": "Nova Osoba"}])
    with TestClient(app) as client:
        login(client)
        home = client.get("/", follow_redirects=False)
        assert home.status_code == 200
        assert "První spuštění není dokončeno" in home.text
        assert "Nova Osoba" in client.get("/technicians").text
        assert "Synchronizovat nyní" not in home.text


@pytest.mark.parametrize("path,data", [
    ("/discovered/1/add", {"email": "a@example.cz", "csrf_token": "x"}),
    ("/discovered/1/ignore", {"csrf_token": "x"}),
    ("/discovered/1/restore", {"csrf_token": "x"}),
    ("/technicians/1/update", {"email": "a@example.cz", "csrf_token": "x"}),
    ("/technicians/1/remove", {"csrf_token": "x"}),
    ("/technicians/1/onboard", {"csrf_token": "x"}),
])
def test_all_technician_mutations_require_admin(path, data, settings):
    app = create_app(settings, FakeGoogle())
    with TestClient(app) as client:
        assert client.post(path, data=data).status_code == 401
        login(client)
        assert client.post(path, data=data).status_code == 403


def test_run_detail_keeps_raw_summary_in_closed_technical_details(settings):
    app = create_app(settings, FakeGoogle()); activate(app)
    with app.state.sessions() as db:
        db.add(SyncRun(mode="apply", status="ok", summary_json='{"issue_samples":[{"raw":"NOISY"}]}'))
        db.commit()
    with TestClient(app) as client:
        login(client); page = client.get("/runs/1")
        assert page.status_code == 200
        assert "<details class=\"card\"><summary>Technické podrobnosti" in page.text
        assert "<details open" not in page.text
        assert page.text.index("NOISY") > page.text.index("Technické podrobnosti")
        assert "E-mail technika slouží" in client.get("/technicians").text


class _Request:
    def __init__(self, result, calls, kwargs): self.result, self.calls, self.kwargs = result, calls, kwargs
    def execute(self, **_kwargs): self.calls.append(self.kwargs); return self.result


class _Calendars:
    def __init__(self, calls): self.calls = calls
    def insert(self, **kwargs): return _Request({"id": "new-cal"}, self.calls, kwargs)


class _Acl:
    def __init__(self, calls, rules): self.calls, self.rules = calls, rules
    def list(self, **kwargs): return _Request({"items": self.rules}, [], kwargs)
    def insert(self, **kwargs): return _Request({}, self.calls, kwargs)


class _CalendarService:
    def __init__(self, calls, rules): self.calls, self.rules = calls, rules
    def calendars(self): return _Calendars(self.calls)
    def acl(self): return _Acl(self.calls, self.rules)


def test_google_onboard_sends_notification_once_and_is_idempotent(tmp_path, monkeypatch):
    client = GoogleClient(tmp_path / "client", tmp_path / "token")
    calls = []
    monkeypatch.setattr(client, "calendar", lambda: _CalendarService(calls, []))
    result = client.onboard_with_prefix("Žák", "z@example.cz", "Calendar Sync – ")
    acl = [x for x in calls if "sendNotifications" in x]
    assert result.invitation_sent is True
    assert len(acl) == 1 and acl[0]["sendNotifications"] is True

    calls.clear()
    rules = [{"role": "reader", "scope": {"type": "user", "value": "Z@example.cz"}}]
    monkeypatch.setattr(client, "calendar", lambda: _CalendarService(calls, rules))
    result = client.onboard_with_prefix("Žák", "z@example.cz", "Calendar Sync – ", "new-cal")
    assert result.acl_already_present is True
    assert result.invitation_sent is False
    assert not calls


class _Events:
    def __init__(self, calls): self.calls = calls
    def insert(self, **kwargs): return _Request({"id": "created"}, self.calls, ("insert", kwargs))
    def update(self, **kwargs): return _Request({"id": "updated"}, self.calls, ("update", kwargs))
    def delete(self, **kwargs): return _Request({}, self.calls, ("delete", kwargs))


class _EventService:
    def __init__(self, calls): self.calls = calls
    def events(self): return _Events(self.calls)


def test_event_writes_disable_updates_and_never_add_attendees(tmp_path, monkeypatch):
    client = GoogleClient(tmp_path / "client", tmp_path / "token")
    calls = []
    monkeypatch.setattr(client, "calendar", lambda: _EventService(calls))
    body = {"summary": "Práce", "start": {"date": "2026-09-21"}, "end": {"date": "2026-09-22"}}
    client.create_event("calendar", body)
    client.update_event("calendar", "event", body)
    client.delete_event("calendar", "event")
    assert [operation for operation, _ in calls] == ["insert", "update", "delete"]
    assert all(kwargs["sendUpdates"] == "none" for _, kwargs in calls)
    assert all("attendees" not in kwargs.get("body", {}) for _, kwargs in calls)
