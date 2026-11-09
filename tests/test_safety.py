from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import time
import zipfile
from types import SimpleNamespace

import pytest
from test_dynamic_parser import workbook

from calendar_sync.google import DriveMeta
from calendar_sync.models import OwnedEvent, Snapshot, SyncRun, Technician, init_db, make_session_factory
from calendar_sync.parser import Event, ParserConfig, TechnicianIdentity, parse_workbook
from calendar_sync.service import ISSUE_SAMPLE_LIMIT, SyncService, bounded_issues, sync_window
from calendar_sync.setup import RuntimeConfig, save_runtime, set_setting


def test_horizon_is_inclusive_and_past_is_ignored(tmp_path):
    path = tmp_path / "horizon.xlsx"
    workbook(path, [(4, "Alice", "10001 TMB x")])
    techs = [TechnicianIdentity("alice", "Alice")]
    events, _ = parse_workbook(
        path, "f", "1", "m", techs, config=ParserConfig(),
        date_from=dt.date(2026, 9, 22), date_through=dt.date(2026, 9, 25),
    )
    assert events == []  # the populated 21 September cell is in the past
    events, _ = parse_workbook(
        path, "f", "1", "m", techs, config=ParserConfig(),
        date_from=dt.date(2026, 9, 21), date_through=dt.date(2026, 9, 21),
    )
    assert len(events) == 1 and events[0].start["date"] == "2026-09-21"


def test_prague_window_uses_local_date_across_dst_boundaries():
    assert sync_window(dt.datetime(2026, 3, 28, 23, 30, tzinfo=dt.UTC), "Europe/Prague", 2) == (
        dt.date(2026, 3, 29), dt.date(2026, 3, 31)
    )
    assert sync_window(dt.datetime(2026, 10, 24, 22, 30, tzinfo=dt.UTC), "Europe/Prague", 1) == (
        dt.date(2026, 10, 25), dt.date(2026, 10, 26)
    )


def test_unmapped_issues_are_deduplicated_and_summaries_bounded(tmp_path):
    path = tmp_path / "issues.xlsx"
    workbook(path, [(row, " Unknown   Person ", "10001 TMB x") for row in range(4, 80)])
    _, issues = parse_workbook(
        path, "f", "1", "m", [TechnicianIdentity("a", "Alice")], config=ParserConfig(),
        date_from=dt.date(2026, 9, 21), date_through=dt.date(2026, 9, 25),
    )
    assert len([x for x in issues if x.get("code") == "unmapped_technician"]) == 1
    summary = bounded_issues([{"code": "x", "n": i} for i in range(ISSUE_SAMPLE_LIMIT + 100)])
    assert summary["issue_count"] == ISSUE_SAMPLE_LIMIT + 100
    assert len(summary["issue_samples"]) == ISSUE_SAMPLE_LIMIT and summary["issues_truncated"] is True


class NoWriteGoogle:
    def __init__(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as archive:
            archive.writestr("placeholder", "x")
        self.payload = buf.getvalue()
        self.write_calls = 0
        self.list_calls = 0

    def metadata(self, _file_id):
        digest = hashlib.md5(self.payload).hexdigest()  # nosec: mirrors Drive integrity metadata
        return DriveMeta("file-1", "source.xlsx", "1", digest, "", str(len(self.payload)))

    def download(self, _file_id, destination):
        destination.write_bytes(self.payload)
        return hashlib.md5(self.payload).hexdigest()  # nosec: mirrors Drive integrity metadata

    def list_owned_events(self, *_args):
        self.list_calls += 1
        return []

    def create_event(self, *_args):
        self.write_calls += 1
        raise AssertionError("Google write must not occur")


def _event() -> Event:
    return Event("Alice", "key", "job", "", None, None, {"date": "2099-01-01"}, {"date": "2099-01-02"}, {"canonical_technician_id": "alice"})


@pytest.mark.parametrize("dry_run, expected_key", [(True, "planned_create_without_calendar"), (False, "skipped_unonboarded")])
def test_unonboarded_technician_is_counted_without_google_calls(tmp_path, settings, monkeypatch, dry_run, expected_key):
    engine, sessions = make_session_factory(settings.database_url)
    init_db(engine)
    with sessions() as db:
        db.add(Technician(canonical_id="alice", workbook_name="Alice", email="a@example.test", enabled=True))
        save_runtime(db, RuntimeConfig(drive_file_id="file-1", sync_horizon_days=90))
        db.commit()
    monkeypatch.setattr("calendar_sync.service.parse_workbook", lambda *_args, **_kwargs: ([_event()], [{"severity": "info", "code": "layout", "layout_fingerprint": "x", "assignments": 1}]))
    google = NoWriteGoogle()
    result = SyncService(settings, sessions, google).sync(dry_run=dry_run)
    assert result[expected_key] == 1
    if dry_run:
        assert result["awaiting_onboarding"] == 1
    assert google.list_calls == 0 and google.write_calls == 0
    with sessions() as db:
        stored = json.loads(db.query(SyncRun).order_by(SyncRun.id.desc()).one().summary_json)
        assert "issue_samples" in stored and len(stored["issue_samples"]) <= ISSUE_SAMPLE_LIMIT


def test_repeated_apply_reuses_stable_snapshot(settings, monkeypatch):
    engine, sessions = make_session_factory(settings.database_url)
    init_db(engine)
    with sessions() as db:
        db.add(Technician(canonical_id="alice", workbook_name="Alice", email="a@example.test", enabled=True))
        save_runtime(db, RuntimeConfig(drive_file_id="file-1", sync_horizon_days=90))
        db.commit()
    monkeypatch.setattr(
        "calendar_sync.service.parse_workbook",
        lambda *_args, **_kwargs: (
            [_event()],
            [{"severity": "info", "code": "layout", "layout_fingerprint": "x", "assignments": 1}],
        ),
    )
    service = SyncService(settings, sessions, NoWriteGoogle())
    assert service.sync(dry_run=False)["status"] == "partial"
    assert service.sync(dry_run=False)["status"] == "partial"
    with sessions() as db:
        assert db.query(Snapshot).count() == 1


def test_poll_forces_sync_for_new_onboarding_with_unchanged_source(settings, monkeypatch):
    engine, sessions = make_session_factory(settings.database_url)
    init_db(engine)
    google = NoWriteGoogle()
    meta = google.metadata("file-1")
    with sessions() as db:
        db.add(Snapshot(fingerprint=meta.fingerprint, drive_version=meta.version, md5=meta.md5, stable=True))
        set_setting(db, "force_sync", True, "admin")
        db.commit()
    service = SyncService(settings, sessions, google)
    service._observed = (meta.fingerprint, time.monotonic() - settings.stability_seconds - 1)
    called = []
    monkeypatch.setattr(service, "sync", lambda **kwargs: called.append(kwargs) or {"status": "ok"})
    assert service.poll()["status"] == "ok"
    assert called == [{"dry_run": False, "expected_fingerprint": meta.fingerprint}]


class ApplyGoogle:
    def __init__(self):
        self.created = []

    def create_event(self, _calendar_id, body):
        self.created.append(body)
        return {"id": "new-google-id"}

    def update_event(self, *_args):
        raise AssertionError("unexpected update")

    def delete_event(self, *_args):
        raise AssertionError("stale event must not be deleted after its first missing check")


def test_first_missing_google_event_initializes_streak_without_type_error(settings):
    engine, sessions = make_session_factory(settings.database_url); init_db(engine)
    with sessions() as db:
        tech = Technician(canonical_id="alice", workbook_name="Alice", calendar_id="calendar-id")
        db.add(tech); db.commit(); tech_id = tech.id
    missing = {"id": "google-id", "extendedProperties": {"private": {"source_key": "missing-key"}}}
    plan = SimpleNamespace(creates=[], updates=[], unchanged=[], missing=[missing])
    SyncService(settings, sessions, ApplyGoogle())._apply_technician(tech, plan, "fingerprint")
    with sessions() as db:
        owned = db.query(OwnedEvent).one()
        assert owned.technician_id == tech_id
        assert (owned.missing_streak, owned.pending_stale) == (1, False)


def test_recreated_event_upserts_existing_mapping_instead_of_duplicate(settings):
    engine, sessions = make_session_factory(settings.database_url); init_db(engine)
    with sessions() as db:
        tech = Technician(canonical_id="alice", workbook_name="Alice", calendar_id="calendar-id")
        db.add(tech); db.flush()
        db.add(OwnedEvent(technician_id=tech.id, source_key="key", google_event_id="deleted-google-id"))
        db.commit()
    event = _event()
    plan = SimpleNamespace(creates=[event], updates=[], unchanged=[], missing=[])
    google = ApplyGoogle()
    SyncService(settings, sessions, google)._apply_technician(tech, plan, "new-fingerprint")
    with sessions() as db:
        rows = db.query(OwnedEvent).all()
        assert len(rows) == 1
        assert rows[0].google_event_id == "new-google-id"
        assert rows[0].last_seen_fingerprint == "new-fingerprint"
        assert (rows[0].missing_streak, rows[0].pending_stale) == (0, False)
