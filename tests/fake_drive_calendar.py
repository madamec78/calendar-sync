"""Test-only in-memory stand-in for Google Drive + Calendar (never talks to the network)."""
from __future__ import annotations

import datetime as dt
import hashlib
import itertools
import threading
import uuid
from pathlib import Path
from urllib.parse import quote

from xlsx_writer import write_xlsx

from calendar_sync.google import DriveMeta, OnboardResult

DEMO_FILE_ID = "demo-planning-file"
_TECHNICIANS = ["Technik Alfa", "Technik Beta", "Technik Gama"]


def _serial(day: dt.date) -> int:
    return (day - dt.date(1899, 12, 30)).days


def _monday(day: dt.date) -> dt.date:
    return day - dt.timedelta(days=day.weekday())


class FakeDriveCalendar:
    """Implements the subset of ``GoogleClient`` used by the application."""

    def __init__(self, today: dt.date | None = None):
        self._lock = threading.RLock()
        self._today = today
        self.version = 1
        self.removed_jobs: set[str] = set()
        self.calendars: dict[str, dict] = {}
        self.acl: dict[str, set[str]] = {}
        self._ids = itertools.count(1)

    # -- identity / auth -------------------------------------------------------------------
    def has_token(self) -> bool: return True
    def credentials(self): return object()
    def bootstrap(self) -> None: return None
    def authorization_url(self, redirect_uri: str, state: str) -> str: return redirect_uri
    def complete_authorization(self, redirect_uri: str, state: str, code: str) -> None: return None

    def check_access(self, file_id: str) -> str:
        return self.metadata(file_id).name

    # -- the "spreadsheet" -------------------------------------------------------------------
    def _build(self, destination: Path) -> None:
        today = self._today or dt.date.today()
        sheets: dict[str, dict[str, object]] = {}
        for week in range(2):
            monday = _monday(today) + dt.timedelta(weeks=week)
            friday = monday + dt.timedelta(days=4)
            name = f"{monday.day}.{monday.month}. - {friday.day}.{friday.month}.{friday.year}"
            cells: dict[str, object] = {"A1": "Plán techniků (demo)"}
            for i in range(5):
                cells[f"{'BCDEF'[i]}3"] = _serial(monday + dt.timedelta(days=i))
            row = 4
            for t_index, tech in enumerate(_TECHNICIANS):
                cells[f"A{row}"] = tech
                for d in range(5):
                    job = 10000 + week * 1000 + t_index * 100 + d
                    if str(job) in self.removed_jobs:
                        continue
                    day = monday + dt.timedelta(days=d)
                    cat = ("TMB", "CTN", "VDF")[(t_index + d) % 3]
                    if (t_index + d) % 2 == 0:
                        cells[f"{'BCDEF'[d]}{row}"] = (f"{job} {cat} Praha, Dlouhá {10 + d} +420 777 55{t_index}{d}00 "
                                                       f"{day.day}.{day.month}.{day.year} 08:00-1{2 + d}:30")
                    else:
                        cells[f"{'BCDEF'[d]}{row}"] = f"{job} {cat} servisní zásah"
                cells[f"{'BCDEF'[(t_index + 1) % 5]}{row + 1}"] = f"Školení bez čísla zakázky ({tech.split()[0]})"
                row += 2
            sheets[name] = cells
        write_xlsx(destination, sheets)

    def simulate_edit(self, remove_job: str | None = None) -> str:
        """Pretend a planner edited the XLSX (new Drive version); optionally drop one job."""
        with self._lock:
            self.version += 1
            if remove_job:
                self.removed_jobs.add(remove_job)
            return str(self.version)

    def first_job(self) -> str | None:
        today = self._today or dt.date.today()  # noqa: F841 - kept for symmetry/readability
        for t_index in range(len(_TECHNICIANS)):
            for d in range(5):
                job = str(10000 + t_index * 100 + d)
                if job not in self.removed_jobs:
                    return job
        return None

    def metadata(self, file_id: str) -> DriveMeta:
        with self._lock:
            tmp = Path(__import__("tempfile").mkdtemp()) / "demo.xlsx"
            self._build(tmp)
            data = tmp.read_bytes()
            tmp.unlink(); tmp.parent.rmdir()
            return DriveMeta(DEMO_FILE_ID, "Plán techniků (demo).xlsx", str(self.version),
                             hashlib.md5(data).hexdigest(), dt.datetime.now(dt.UTC).isoformat(), str(len(data)))  # nosec

    def download(self, file_id: str, destination: Path) -> str:
        with self._lock:
            destination.parent.mkdir(parents=True, exist_ok=True)
            self._build(destination)
            return hashlib.md5(destination.read_bytes()).hexdigest()  # nosec

    # -- calendars ---------------------------------------------------------------------------
    def _calendar(self, calendar_id: str) -> dict:
        return self.calendars.setdefault(calendar_id, {})

    def list_owned_events(self, calendar_id: str, file_id: str) -> list[dict]:
        with self._lock:
            return [dict(e) for e in self._calendar(calendar_id).values()
                    if ((e.get("extendedProperties") or {}).get("private") or {}).get("source_file_id") == file_id]

    def create_event(self, calendar_id: str, body: dict) -> dict:
        with self._lock:
            event_id = f"demo{next(self._ids):06d}{uuid.uuid4().hex[:6]}"
            event = {**body, "id": event_id}
            self._calendar(calendar_id)[event_id] = event
            return dict(event)

    def update_event(self, calendar_id: str, event_id: str, body: dict) -> dict:
        with self._lock:
            events = self._calendar(calendar_id)
            if event_id not in events:
                from googleapiclient.errors import HttpError
                from httplib2 import Response
                raise HttpError(Response({"status": 404}), b"not found")
            events[event_id] = {**body, "id": event_id}
            return dict(events[event_id])

    def delete_event(self, calendar_id: str, event_id: str) -> None:
        with self._lock:
            self._calendar(calendar_id).pop(event_id, None)

    def onboard(self, name: str, email: str, existing_calendar_id: str | None = None) -> OnboardResult:
        return self.onboard_with_prefix(name, email, "Calendar Sync – ", existing_calendar_id)

    def onboard_with_prefix(self, name: str, email: str, prefix: str, existing_calendar_id: str | None = None,
                            timezone_name: str = "Europe/Prague", summary: str | None = None) -> OnboardResult:
        with self._lock:
            calendar_id = existing_calendar_id or f"demo-{hashlib.sha1((summary or prefix + name).encode()).hexdigest()[:12]}@group.calendar.google.test"  # nosec
            self._calendar(calendar_id)
            readers = self.acl.setdefault(calendar_id, set())
            exists = email.casefold() in readers
            readers.add(email.casefold())
            link = "https://calendar.google.com/calendar/u/0/r?cid=" + quote(calendar_id, safe="")
            return OnboardResult(calendar_id, link, not exists, exists)
