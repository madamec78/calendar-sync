from __future__ import annotations

import datetime as dt
import json
import logging
import tempfile
import threading
import time
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from googleapiclient.errors import HttpError
from sqlalchemy import select

from .config import Settings
from .discovery import record_discovered
from .google import GoogleClient, OAuthReauthorizationRequired, OAuthTransientError
from .models import AuditLog, OwnedEvent, Snapshot, SyncRun, Technician
from .parser import ParserConfig, TechnicianIdentity, parse_workbook
from .reconcile import next_stale_state, plan
from .setup import get_setting, load_runtime, set_setting

log = logging.getLogger("calendar_sync")

ISSUE_SAMPLE_LIMIT = 50

def bounded_issues(issues: list[dict]) -> dict:
    """Return stable counts plus a bounded diagnostic sample for UI/audit storage."""
    counts: dict[str, int] = {}
    for issue in issues:
        code = str(issue.get("code", "unknown")); counts[code] = counts.get(code, 0) + 1
    return {"issue_count": len(issues), "issue_counts": counts, "issue_samples": issues[:ISSUE_SAMPLE_LIMIT], "issues_truncated": len(issues) > ISSUE_SAMPLE_LIMIT}


def describe_google_error(exc: Exception) -> str:
    """Short, secret-free description of a Google/API failure for the UI."""
    if isinstance(exc, HttpError):
        status = getattr(exc.resp, "status", "?")
        return f"Google API HTTP {status}"
    return type(exc).__name__


def sync_window(now: dt.datetime, timezone: str, horizon_days: int, lookback_days: int = 0) -> tuple[dt.date, dt.date]:
    """Compute inclusive local-calendar bounds, independent of UTC and DST transitions."""
    today = now.astimezone(ZoneInfo(timezone)).date()
    return today - dt.timedelta(days=lookback_days), today + dt.timedelta(days=horizon_days)


class SyncService:
    def __init__(self, settings: Settings, session_factory, google: GoogleClient):
        self.settings = settings; self.sessions = session_factory; self.google = google; self.lock = threading.Lock()
        self.timezone = "Europe/Prague"
        self._observed: tuple[str, float] | None = None

    def poll(self) -> dict:
        meta = self.google.metadata(self.settings.source_file_id)
        now = time.monotonic()
        if not self._observed or self._observed[0] != meta.fingerprint:
            self._observed = (meta.fingerprint, now)
            return {"status": "debouncing", "fingerprint": meta.fingerprint}
        if now - self._observed[1] < self.settings.stability_seconds:
            return {"status": "debouncing", "fingerprint": meta.fingerprint}
        with self.sessions() as db:
            force_sync = bool(get_setting(db, "force_sync", False))
            if db.scalar(select(Snapshot).where(Snapshot.fingerprint == meta.fingerprint)) and not force_sync:
                return {"status": "unchanged", "fingerprint": meta.fingerprint}
        return self.sync(dry_run=False, expected_fingerprint=meta.fingerprint)

    def sync(self, dry_run: bool, expected_fingerprint: str | None = None) -> dict:
        if not self.lock.acquire(blocking=False):
            return {"status": "busy"}
        run = None
        try:
            with self.sessions() as db:
                run = SyncRun(mode="dry-run" if dry_run else "apply"); db.add(run); db.commit(); run_id = run.id
            before = self.google.metadata(self.settings.source_file_id)
            if expected_fingerprint and before.fingerprint != expected_fingerprint:
                return self._finish(run_id, "deferred", {"reason": "source changed before download"})
            with tempfile.TemporaryDirectory(dir=self.settings.data_dir) as work:
                source = Path(work) / "source.xlsx"
                digest = self.google.download(self.settings.source_file_id, source)
                if not zipfile.is_zipfile(source): raise RuntimeError("download is not a valid XLSX ZIP")
                if before.md5 and digest != before.md5: raise RuntimeError("download MD5 differs from Drive metadata")
                after = self.google.metadata(self.settings.source_file_id)
                if before.fingerprint != after.fingerprint:
                    return self._finish(run_id, "deferred", {"reason": "source changed during download"})
                with self.sessions() as db:
                    technicians = list(db.scalars(select(Technician).where(Technician.enabled.is_(True))))
                    runtime = load_runtime(db)
                    previous_layout = get_setting(db, "layout_fingerprint")
                    previous_assignments = int(get_setting(db, "assignment_count", 0) or 0)
                    previous_window = get_setting(db, "sync_window")
                import json as _json
                identities = [TechnicianIdentity(x.canonical_id or f"legacy-{x.id}", x.workbook_name, tuple(_json.loads(x.aliases_json or "[]"))) for x in technicians]
                parser_config = ParserConfig(workbook_profile=runtime.deployment_name, sheet_pattern=runtime.sheet_pattern,
                    technician_name_column=runtime.technician_name_column, header_scan_rows=runtime.header_scan_rows,
                    first_data_column=runtime.first_data_column or None, auto_detect_dates=runtime.auto_detect_dates,
                    continuation_policy=runtime.continuation_policy, color_map=runtime.color_map,
                    parse_phones=runtime.parse_phones, parse_addresses=runtime.parse_addresses)
                date_from, date_through = sync_window(dt.datetime.now(dt.UTC), runtime.timezone, runtime.sync_horizon_days, runtime.sync_lookback_days)
                current_window = {"from": date_from.isoformat(), "through": date_through.isoformat()}
                if previous_window != current_window:
                    previous_layout = None
                    previous_assignments = 0
                parsed, issues = parse_workbook(source, before.id, before.version, digest, identities, self.timezone, parser_config, date_from, date_through)
                with self.sessions() as db:
                    record_discovered(db, issues)
            layout = next((x for x in issues if x.get("code") == "layout"), {})
            fatal = [x for x in issues if x.get("severity") == "fatal"]
            assignment_count = int(layout.get("assignments", 0))
            if previous_assignments and assignment_count < previous_assignments * (1 - runtime.assignment_drop_threshold):
                fatal.append({"severity": "fatal", "code": "assignment_drop", "previous": previous_assignments, "current": assignment_count})
            if previous_layout and layout.get("layout_fingerprint") != previous_layout:
                fatal.append({"severity": "fatal", "code": "layout_changed", "previous": previous_layout, "current": layout.get("layout_fingerprint")})
            if fatal:
                with self.sessions() as db:
                    db.add(AuditLog(actor="worker", action="layout_drift_blocked", details_json=json.dumps(bounded_issues(fatal), ensure_ascii=False))); db.commit()
                return self._finish(run_id, "drift_blocked", {**bounded_issues(issues), "deletions_suppressed": True, "fingerprint": before.fingerprint})
            by_tech = defaultdict(list)
            for event in parsed: by_tech[event.private["canonical_technician_id"]].append(event)
            totals: dict[str, Any] = {"create": 0, "update": 0, "unchanged": 0, "pending_stale": 0, "delete": 0, "foreign_untouched": 0, "awaiting_onboarding": 0, "planned_create_without_calendar": 0, "skipped_unonboarded": 0}
            awaiting: list[str] = []; failed: list[str] = []
            for tech in technicians:
                canonical_id = tech.canonical_id or f"legacy-{tech.id}"
                if not tech.calendar_id:
                    event_count = len(by_tech[canonical_id])
                    if dry_run:
                        totals["awaiting_onboarding"] = int(totals["awaiting_onboarding"]) + 1
                        totals["planned_create_without_calendar"] = int(totals["planned_create_without_calendar"]) + event_count
                    else:
                        totals["skipped_unonboarded"] = int(totals["skipped_unonboarded"]) + event_count
                    awaiting.append(tech.workbook_name)
                    continue
                try:
                    existing = self.google.list_owned_events(tech.calendar_id, before.id)
                    work_plan = plan(by_tech[canonical_id], existing, before.id, tech.workbook_name, canonical_id)
                    totals["create"] += len(work_plan.creates); totals["update"] += len(work_plan.updates)
                    totals["unchanged"] += len(work_plan.unchanged); totals["foreign_untouched"] += len(work_plan.foreign)
                    if not dry_run:
                        self._apply_technician(tech, work_plan, before.fingerprint)
                    else:
                        with self.sessions() as db:
                            for missing in work_plan.missing:
                                owned = db.scalar(select(OwnedEvent).where(OwnedEvent.technician_id == tech.id, OwnedEvent.source_key == self._key(missing)))
                                if owned and owned.missing_fingerprint != before.fingerprint and owned.missing_streak + 1 >= 2: totals["pending_stale"] += 1
                except Exception as exc:
                    if isinstance(exc, (OAuthReauthorizationRequired, OAuthTransientError)):
                        raise
                    failed.append(tech.workbook_name)
                    log.exception("technician_sync_failed", extra={"technician": tech.workbook_name})
                    with self.sessions() as db:
                        current = db.get(Technician, tech.id)
                        if current is not None:
                            current.last_error = describe_google_error(exc); db.commit()
            if not dry_run:
                with self.sessions() as db:
                    snap = db.scalar(select(Snapshot).where(Snapshot.fingerprint == before.fingerprint))
                    if snap is None:
                        db.add(Snapshot(fingerprint=before.fingerprint, drive_version=before.version, md5=digest, stable=True))
                    else:
                        snap.stable = True
                    set_setting(db, "layout_fingerprint", layout.get("layout_fingerprint"), "worker")
                    set_setting(db, "assignment_count", assignment_count, "worker")
                    set_setting(db, "sync_window", current_window, "worker")
                    # Admin changes request one extra pass over an unchanged snapshot; it is consumed here.
                    # Stale confirmation deliberately does NOT use it: that needs a *different* snapshot.
                    set_setting(db, "force_sync", bool(failed), "worker")
                    db.commit()
                    totals["pending_stale"] = len(list(db.scalars(select(OwnedEvent).where(OwnedEvent.pending_stale.is_(True)))))
            totals["awaiting_onboarding_samples"] = awaiting[:20]
            totals["awaiting_onboarding_truncated"] = len(awaiting) > 20
            totals["failed_technicians"] = failed[:20]
            totals["horizon"] = {"from": date_from.isoformat(), "through": date_through.isoformat()}
            final_status = "partial" if (not dry_run and totals["skipped_unonboarded"]) or failed else "ok"
            return self._finish(run_id, final_status, {**totals, **bounded_issues(issues), "fingerprint": before.fingerprint})
        except Exception as exc:
            log.exception("sync_failed", extra={"event": "sync_failed"})
            if run is not None:
                self._finish(run.id, "error", {}, str(exc))
            raise
        finally:
            self.lock.release()

    def _apply_technician(self, tech: Technician, work_plan, fingerprint: str) -> None:
        if not tech.calendar_id:
            raise ValueError("technician must be onboarded before apply")
        calendar_id = tech.calendar_id
        with self.sessions() as db:
            for event in work_plan.creates:
                created = self.google.create_event(calendar_id, event.google_body())
                owned = self._owned_event(db, tech.id, event.source_key, created["id"])
                owned.missing_streak = 0; owned.pending_stale = False; owned.last_seen_fingerprint = fingerprint
                owned.missing_fingerprint = None
                db.commit()  # persist the mapping immediately; a later failure must not orphan it
            seen: list[tuple[str, str]] = []
            for old, event in work_plan.updates:
                event_id = old["id"]
                try:
                    self.google.update_event(calendar_id, event_id, event.google_body())
                except HttpError as exc:
                    if getattr(exc.resp, "status", None) not in (404, 410):
                        raise
                    # The event vanished between list and update (deleted by a user): recreate it.
                    event_id = self.google.create_event(calendar_id, event.google_body())["id"]
                seen.append((event.source_key, event_id))
            seen.extend((event.source_key, old["id"]) for old, event in work_plan.unchanged)
            for source_key, event_id in seen:
                owned = self._owned_event(db, tech.id, source_key, event_id)
                owned.missing_streak = 0; owned.pending_stale = False; owned.last_seen_fingerprint = fingerprint
                owned.missing_fingerprint = None
            stale_tracking = self._stale_tracking_enabled()
            for missing in work_plan.missing:
                key = self._key(missing)
                owned = self._owned_event(db, tech.id, key, missing["id"])
                if not stale_tracking:
                    owned.missing_streak = 0; owned.pending_stale = False
                    continue
                counted = owned.missing_fingerprint == fingerprint
                owned.missing_streak, owned.pending_stale = next_stale_state(owned.missing_streak, True, already_counted=counted)
                owned.missing_fingerprint = fingerprint
                if owned.pending_stale and self.settings.delete_enabled:
                    # IDs are selected only from events bearing both our file and technician private properties.
                    try:
                        self.google.delete_event(calendar_id, owned.google_event_id)
                    except HttpError as exc:
                        if getattr(exc.resp, "status", None) not in (404, 410):
                            raise
                    db.delete(owned)
            current = db.get(Technician, tech.id); current.last_sync_at = dt.datetime.now(dt.UTC); current.last_error = None
            db.commit()

    def _stale_tracking_enabled(self) -> bool:
        with self.sessions() as db:
            return load_runtime(db).stale_policy != "disabled"

    @staticmethod
    def _owned_event(db, technician_id: int, source_key: str, google_event_id: str) -> OwnedEvent:
        """Return one durable local mapping and update it when Google recreated the event."""
        owned = db.scalar(select(OwnedEvent).where(OwnedEvent.technician_id == technician_id, OwnedEvent.source_key == source_key))
        if owned is None:
            owned = OwnedEvent(
                technician_id=technician_id,
                source_key=source_key,
                google_event_id=google_event_id,
                missing_streak=0,
                pending_stale=False,
            )
            db.add(owned)
        else:
            owned.google_event_id = google_event_id
        return owned

    @staticmethod
    def _key(event: dict) -> str:
        return (((event.get("extendedProperties") or {}).get("private") or {}).get("source_key") or "")

    def _finish(self, run_id: int, status: str, summary: dict, error: str | None = None) -> dict:
        with self.sessions() as db:
            run = db.get(SyncRun, run_id); run.status = status; run.finished_at = dt.datetime.now(dt.UTC)
            run.fingerprint = summary.get("fingerprint"); run.summary_json = json.dumps(summary, ensure_ascii=False); run.error = error; db.commit()
        return {"status": status, "run_id": run_id, **summary}
