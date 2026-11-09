from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import hmac
import json
import logging
import re
import secrets
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest
from sqlalchemy import func, select, text
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import Response

from .auth import MIN_PASSWORD_LENGTH, password_problems
from .backup import create_backup
from .config import Settings
from .discovery import normalized_name, record_discovered
from .google import (
    ClientConfigError,
    GoogleClient,
    OAuthReauthorizationRequired,
    OAuthTransientError,
    client_id_hint,
    install_client_file,
    validate_client_config,
)
from .models import (
    AuditLog,
    DiscoveredTechnician,
    OwnedEvent,
    SyncRun,
    Technician,
    init_db,
    make_session_factory,
)
from .parser import ParserConfig, TechnicianIdentity, parse_workbook
from .service import SyncService
from .setup import (
    RuntimeConfig,
    calendar_name,
    extract_drive_id,
    get_setting,
    load_runtime,
    material_changes,
    next_backup,
    next_sync,
    save_runtime,
    set_admin_password,
    set_setting,
    setup_complete,
    verify_admin_password,
)

SYNC_COUNT = Counter("calendar_sync_requests_total", "Requested synchronizations", ["mode", "result"])
log = logging.getLogger("calendar_sync")

PACKAGE_DIR = Path(__file__).parent
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}
VERSION = "2.0.0"


class LoginThrottle:
    """Tiny in-memory brute-force guard for the single admin password."""

    def __init__(self, limit: int = 6, window: int = 300):
        self.limit, self.window = limit, window
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str) -> list[float]:
        cutoff = time.monotonic() - self.window
        recent = [x for x in self._failures.get(key, []) if x > cutoff]
        self._failures[key] = recent
        return recent

    def blocked(self, key: str) -> bool:
        with self._lock:
            return len(self._recent(key)) >= self.limit

    def fail(self, key: str) -> None:
        with self._lock:
            self._recent(key).append(time.monotonic())

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)


def _parse_dt(value) -> dt.datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, dt.datetime):
        parsed = value
    else:
        try:
            parsed = dt.datetime.fromisoformat(str(value))
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


async def read_form(request: Request) -> dict[str, str]:
    form = await request.form()
    return {key: value for key, value in form.items() if isinstance(value, str)}


def _int(form: dict[str, str], key: str, default: int) -> int:
    try:
        return int(form.get(key, "").strip())
    except ValueError:
        return default


def _float(form: dict[str, str], key: str, default: float) -> float:
    try:
        return float(form.get(key, "").strip().replace(",", "."))
    except ValueError:
        return default


def create_app(settings: Settings | None = None, google=None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    engine, sessions = make_session_factory(settings.database_url)
    init_db(engine)
    if google is None:
        google = GoogleClient(settings.oauth_client_file, settings.oauth_token_file)
    uploaded_client_file = settings.data_dir / "google_oauth_client.json"

    def effective_client_file() -> Path:
        """Mounted/secret file wins; otherwise the file uploaded through the UI (stored in the data volume)."""
        return settings.oauth_client_file if settings.oauth_client_file.exists() else uploaded_client_file

    if isinstance(google, GoogleClient):
        google.client_file = effective_client_file()

    def client_status() -> dict:
        path = effective_client_file()
        if not path.exists():
            return {"present": False, "hint": "", "uploaded": False}
        try:
            hint = client_id_hint(validate_client_config(path.read_text(encoding="utf-8")))
        except (OSError, ClientConfigError):
            return {"present": True, "hint": "", "invalid": True, "uploaded": path == uploaded_client_file}
        return {"present": True, "hint": hint, "uploaded": path == uploaded_client_file}
    service = SyncService(settings, sessions, google)
    scheduler = BackgroundScheduler(timezone="UTC")
    throttle = LoginThrottle()

    # ------------------------------------------------------------------ helpers
    def check_access(file_id: str) -> str:
        """Drive + Calendar reachability test; returns the source file name."""
        checker = getattr(google, "check_access", None)
        if callable(checker):
            return checker(file_id)
        meta = google.metadata(file_id)
        google.calendar().calendarList().list(maxResults=1).execute()
        return getattr(meta, "name", file_id)

    def has_token() -> bool:
        checker = getattr(google, "has_token", None)
        return bool(checker()) if callable(checker) else settings.oauth_token_file.exists()

    def oauth_ui_status() -> tuple[str, str, bool]:
        """(severity, human text, needs recovery)"""
        if not has_token():
            return "warn", "chybí – je nutná autorizace", True
        try:
            google.credentials()
        except OAuthReauthorizationRequired:
            return "bad", "neplatná – je nutná nová autorizace", True
        except Exception:  # noqa: BLE001 - UI exposes only a sanitized state
            return "bad", "nelze použít", True
        return "good", "použitelná", False

    def worker_error(exc: Exception) -> dict[str, str]:
        if isinstance(exc, OAuthReauthorizationRequired):
            return {"code": "oauth_reauthorization_required",
                    "message": "Google OAuth autorizace vypršela nebo byla odvolána. Obnovte ji v Nastavení."}
        if isinstance(exc, OAuthTransientError):
            return {"code": "oauth_transient_error",
                    "message": "Dočasná chyba Google OAuth. Služba to zkusí znovu v běžném intervalu."}
        return {"code": "worker_error", "message": f"Synchronizace selhala ({type(exc).__name__})."}

    def set_worker_error(error: dict[str, str] | None) -> None:
        with sessions() as db:
            set_setting(db, "worker_last_error", error, "worker")
            db.commit()

    def prepare_service(cfg: RuntimeConfig, *, allow_delete: bool | None = None) -> None:
        service.settings = dataclasses.replace(
            settings, source_file_id=cfg.drive_file_id, stability_seconds=cfg.stability_seconds,
            delete_enabled=cfg.delete_enabled if allow_delete is None else allow_delete)
        service.timezone = cfg.timezone

    def schedule_resync(db) -> None:
        """Ask the worker for one extra pass over an unchanged snapshot (after admin changes)."""
        set_setting(db, "force_sync", True, "admin")
        set_setting(db, "next_sync_at", dt.datetime.now(dt.UTC).isoformat(), "admin")

    def store_next_times(db, cfg: RuntimeConfig, now: dt.datetime) -> None:
        set_setting(db, "next_sync_at", next_sync(now, cfg).astimezone(dt.UTC).isoformat())
        following = next_backup(now, cfg)
        set_setting(db, "next_backup_at", following.astimezone(dt.UTC).isoformat() if following else None)

    def audit(db, action: str, **details) -> None:
        db.add(AuditLog(actor="admin", action=action, details_json=json.dumps(details, ensure_ascii=False)))

    # ------------------------------------------------------------------ scheduler
    def scheduled_tick():
        now = dt.datetime.now(dt.UTC)
        with sessions() as db:
            if not setup_complete(db):
                return
            runtime = load_runtime(db)
            due = get_setting(db, "next_sync_at")
            backup_due = get_setting(db, "next_backup_at")
            last_run_id = db.scalar(select(SyncRun.id).order_by(SyncRun.id.desc()).limit(1)) or 0
            if not due:  # self-heal: an active worker always has a next run
                set_setting(db, "next_sync_at", next_sync(now, runtime).astimezone(dt.UTC).isoformat(), "worker")
                db.commit()
                due = get_setting(db, "next_sync_at")
        due_at = _parse_dt(due)
        if due_at and now >= due_at:
            prepare_service(runtime)
            try:
                result = service.poll()
            except Exception as exc:
                error = worker_error(exc)
                if isinstance(exc, (OAuthReauthorizationRequired, OAuthTransientError)):
                    log.error("scheduled_sync_failed", extra={"error_code": error["code"]})
                else:
                    log.exception("scheduled_sync_failed", extra={"error_code": error["code"]})
                with sessions() as db:
                    current_run_id = db.scalar(select(SyncRun.id).order_by(SyncRun.id.desc()).limit(1)) or 0
                    if current_run_id <= last_run_id:
                        db.add(SyncRun(mode="scheduled", status="error", finished_at=now,
                                       summary_json="{}", error=error["code"]))
                    set_setting(db, "worker_last_error", error, "worker")
                    set_setting(db, "next_sync_at", next_sync(now, runtime).astimezone(dt.UTC).isoformat(), "worker")
                    db.commit()
            else:
                if result.get("status") not in {"debouncing", "busy"}:
                    with sessions() as db:
                        if result.get("status") == "error":
                            set_setting(db, "worker_last_error", {"code": "worker_error", "message": "Synchronizace selhala."}, "worker")
                        else:
                            set_setting(db, "worker_last_error", None, "worker")
                        set_setting(db, "next_sync_at", next_sync(now, runtime).astimezone(dt.UTC).isoformat(), "worker")
                        db.commit()
        backup_at = _parse_dt(backup_due)
        if backup_at and now >= backup_at:
            try:
                create_backup(settings.database_url, settings.data_dir, runtime, now)
            except Exception as exc:
                log.exception("backup_failed")
                with sessions() as db:
                    db.add(AuditLog(actor="worker", action="backup_failed",
                                    details_json=json.dumps({"error": type(exc).__name__})))
                    db.commit()
            with sessions() as db:
                following = next_backup(now, runtime)
                set_setting(db, "next_backup_at", following.astimezone(dt.UTC).isoformat() if following else None, "worker")
                db.commit()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        errors = settings.validate_runtime()
        if errors:
            log.error("configuration_invalid", extra={"errors": errors})
        else:
            scheduler.add_job(scheduled_tick, "interval", seconds=30, max_instances=1, coalesce=True, id="schedule-tick")
            scheduler.start()
        yield
        if scheduler.running:
            scheduler.shutdown(wait=False)

    app = FastAPI(title="Calendar Sync", version=VERSION, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(SessionMiddleware, secret_key=settings.session_secret or secrets.token_urlsafe(32),
                       same_site="strict", https_only=settings.cookie_secure, max_age=8 * 3600)
    app.state.settings = settings
    app.state.sessions = sessions
    app.state.service = service
    app.state.google = google
    app.state.scheduled_tick = scheduled_tick
    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")
    templates = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))
    prague = ZoneInfo("Europe/Prague")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "frame-ancestors 'none'; form-action 'self' https://accounts.google.com; base-uri 'none'")
        return response

    # ------------------------------------------------------------------ templating
    def fmt_dt(value, tz_name: str = "Europe/Prague", with_seconds: bool = False) -> str:
        parsed = _parse_dt(value)
        if parsed is None:
            return "–"
        try:
            zone = ZoneInfo(tz_name)
        except Exception:  # noqa: BLE001
            zone = prague
        return parsed.astimezone(zone).strftime("%d. %m. %Y %H:%M" + (":%S" if with_seconds else ""))

    def rel_dt(value) -> str:
        parsed = _parse_dt(value)
        if parsed is None:
            return ""
        seconds = int((parsed - dt.datetime.now(dt.UTC)).total_seconds())
        future = seconds >= 0
        seconds = abs(seconds)
        if seconds < 60:
            text_ = "za chvíli" if future else "před chvílí"
        elif seconds < 3600:
            text_ = f"{seconds // 60} min"
        elif seconds < 86400:
            text_ = f"{seconds // 3600} h"
        else:
            text_ = f"{seconds // 86400} d"
        if seconds < 60:
            return text_
        return f"za {text_}" if future else f"před {text_}"

    templates.env.filters["dt"] = fmt_dt
    templates.env.filters["reldt"] = rel_dt
    templates.env.filters["pretty_json"] = lambda v: json.dumps(v, ensure_ascii=False, indent=2, default=str)

    def csrf(request: Request) -> str:
        token = request.session.get("csrf")
        if not token:
            token = secrets.token_urlsafe(32)
            request.session["csrf"] = token
        return token

    def require_csrf(request: Request, token: str | None) -> None:
        if not token or not hmac.compare_digest(request.session.get("csrf", ""), token):
            raise HTTPException(403, "CSRF validation failed")

    def require_auth(request: Request) -> None:
        if not request.session.get("authenticated"):
            raise HTTPException(401, "Login required")

    def flash(request: Request, kind: str, message: str) -> None:
        request.session["flash"] = [*request.session.get("flash", []), [kind, message]][-5:]

    def render(request: Request, name: str, status_code: int = 200, **context) -> HTMLResponse:
        flashes = request.session.pop("flash", [])
        base = {"csrf_token": csrf(request), "flashes": flashes, "min_password": MIN_PASSWORD_LENGTH, "version": VERSION,
                "authenticated": bool(request.session.get("authenticated")), "path": request.url.path}
        return templates.TemplateResponse(request, name, {**base, **context}, status_code=status_code)

    def message_page(request: Request, title: str, message: str, kind: str = "ok", status_code: int = 200,
                     back: str = "/", back_label: str = "Zpět") -> HTMLResponse:
        return render(request, "message.html", status_code, title=title, message=message, kind=kind,
                      back=back, back_label=back_label)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        if exc.status_code == 401 and request.method == "GET":
            return RedirectResponse("/login", status_code=303)
        accept = request.headers.get("accept", "")
        if "text/html" in accept:
            return message_page(request, f"Chyba {exc.status_code}", str(exc.detail), "bad", exc.status_code)
        return Response(json.dumps({"detail": exc.detail}), status_code=exc.status_code, media_type="application/json")

    # ------------------------------------------------------------------ health
    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz():
        errors = settings.validate_runtime()
        try:
            with sessions() as db:
                db.execute(text("select 1"))
        except Exception as exc:  # noqa: BLE001 - readiness must report any DB driver failure
            errors.append(f"database: {type(exc).__name__}")
        if not has_token():
            errors.append("OAuth token missing")
        else:
            try:
                google.credentials()
            except OAuthReauthorizationRequired:
                errors.append("Google OAuth reauthorization required")
            except Exception as exc:  # noqa: BLE001 - return only a sanitized classification
                errors.append(f"OAuth credentials unusable: {type(exc).__name__}")
        with sessions() as db:
            if not setup_complete(db):
                errors.append("First-run setup incomplete")
            worker_failure = get_setting(db, "worker_last_error")
            if worker_failure:
                errors.append(str(worker_failure.get("message", "Latest worker run failed")))
        if errors:
            return Response(json.dumps({"status": "not_ready", "errors": errors}), status_code=503, media_type="application/json")
        return {"status": "ready"}

    @app.get("/metrics")
    def metrics():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    # ------------------------------------------------------------------ auth
    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        if request.session.get("authenticated"):
            return RedirectResponse("/", 303)
        return render(request, "login.html")

    @app.post("/login")
    def login(request: Request, form: dict = Depends(read_form)):
        require_csrf(request, form.get("csrf_token"))
        client = request.client.host if request.client else "unknown"
        if throttle.blocked(client):
            return render(request, "login.html", 429, error="Příliš mnoho neúspěšných pokusů. Zkuste to za několik minut.")
        password = form.get("password", "")
        with sessions() as db:
            valid = verify_admin_password(db, password, settings.admin_password)
        if not valid:
            throttle.fail(client)
            return render(request, "login.html", 401, error="Neplatné heslo.")
        throttle.reset(client)
        request.session.clear()
        request.session["authenticated"] = True
        csrf(request)
        return RedirectResponse("/", status_code=303)

    @app.post("/logout")
    def logout(request: Request, form: dict = Depends(read_form)):
        require_auth(request)
        require_csrf(request, form.get("csrf_token"))
        request.session.clear()
        return RedirectResponse("/login", 303)

    # ------------------------------------------------------------------ OAuth
    def oauth_redirect_uri() -> tuple[str, bool]:
        """(redirect uri, is direct callback). A local admin URL can receive the callback itself."""
        base = settings.public_base_url.rstrip("/")
        host = urlparse(base).hostname or ""
        if host in LOCAL_HOSTS or host == "localhost":
            return base + "/oauth/callback", True
        return "http://localhost:8081/", False

    @app.get("/oauth/authorize")
    def oauth_authorize(request: Request):
        require_auth(request)
        state = secrets.token_urlsafe(32)
        redirect_uri, direct = oauth_redirect_uri()
        request.session["oauth_state"] = state
        request.session["oauth_redirect_uri"] = redirect_uri
        try:
            url = google.authorization_url(redirect_uri, state)
        except Exception as exc:  # noqa: BLE001 - Google client/config errors are heterogeneous
            log.warning("oauth_authorization_failed", extra={"error": type(exc).__name__})
            return message_page(request, "Google OAuth", "Autorizační odkaz se nepodařilo připravit. "
                                "Zkontrolujte, že existuje soubor s OAuth klientem (google_oauth_client.json).",
                                "bad", 500, "/settings", "Zpět do nastavení")
        if not direct:
            request.session["oauth_manual"] = True
        return RedirectResponse(url, status_code=302)

    @app.get("/oauth/manual", response_class=HTMLResponse)
    def oauth_manual_form(request: Request):
        require_auth(request)
        return render(request, "oauth_manual.html")

    @app.post("/oauth/manual", response_class=HTMLResponse)
    def oauth_manual_complete(request: Request, form: dict = Depends(read_form)):
        require_auth(request)
        require_csrf(request, form.get("csrf_token"))
        expected = request.session.pop("oauth_state", None)
        redirect_uri = request.session.pop("oauth_redirect_uri", "http://localhost:8081/")
        parsed = urlparse(form.get("callback_url", "").strip())
        values = parse_qs(parsed.query)
        state = values.get("state", [None])[0]
        code = values.get("code", [None])[0]
        if parsed.hostname not in {"localhost", "127.0.0.1"} or not expected or not state \
                or not hmac.compare_digest(expected, state) or not code:
            raise HTTPException(403, "OAuth callback není platný nebo neodpovídá zahájené autorizaci")
        try:
            google.complete_authorization(redirect_uri, expected, code)
        except Exception as exc:  # noqa: BLE001
            log.warning("oauth_manual_callback_failed", extra={"error": type(exc).__name__})
            return message_page(request, "Google OAuth", "Autorizaci se nepodařilo dokončit. Spusťte ji znovu.",
                                "bad", 502, "/settings", "Zpět do nastavení")
        set_worker_error(None)
        return message_page(request, "Google OAuth", "Google OAuth byl bezpečně uložen.", "ok", 200, "/settings", "Zpět do nastavení")

    @app.get("/oauth/callback", response_class=HTMLResponse)
    def oauth_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
        require_auth(request)
        expected = request.session.pop("oauth_state", None)
        redirect_uri = request.session.pop("oauth_redirect_uri", None) or settings.public_base_url.rstrip("/") + "/oauth/callback"
        if not expected or not state or not hmac.compare_digest(expected, state):
            raise HTTPException(403, "OAuth state validation failed")
        if error or not code:
            return message_page(request, "Google OAuth", "Google OAuth nebyl dokončen.", "bad", 400, "/settings", "Zpět do nastavení")
        try:
            google.complete_authorization(redirect_uri, expected, code)
        except Exception as exc:  # noqa: BLE001 - Google SDK errors are heterogeneous
            log.warning("oauth_callback_failed", extra={"error": type(exc).__name__})
            return message_page(request, "Google OAuth", "Google OAuth se nepodařilo dokončit. Token nebyl zobrazen.",
                                "bad", 502, "/settings", "Zpět do nastavení")
        set_worker_error(None)
        return message_page(request, "Google OAuth", "Google OAuth byl bezpečně uložen.", "ok", 200, "/settings", "Zpět do nastavení")

    # ------------------------------------------------------------------ dashboard
    def load_runs(db, limit: int) -> list[dict]:
        rows = []
        for run in db.scalars(select(SyncRun).order_by(SyncRun.id.desc()).limit(limit)):
            try:
                summary = json.loads(run.summary_json or "{}")
            except ValueError:
                summary = {}
            rows.append({"run": run, "summary": summary})
        return rows

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        if not request.session.get("authenticated"):
            return RedirectResponse("/login", status_code=303)
        with sessions() as db:
            complete = setup_complete(db)
            if not complete and not db.scalar(select(DiscoveredTechnician.id).limit(1)):
                return RedirectResponse("/setup", 303)
            cfg = load_runtime(db)
            technicians = list(db.scalars(select(Technician).order_by(Technician.workbook_name)))
            pending = db.scalar(select(func.count()).select_from(OwnedEvent).where(OwnedEvent.pending_stale.is_(True))) or 0
            tracked = db.scalar(select(func.count()).select_from(OwnedEvent)) or 0
            runs = load_runs(db, 6)
            next_sync_at = get_setting(db, "next_sync_at")
            next_backup_at = get_setting(db, "next_backup_at")
            worker_failure = get_setting(db, "worker_last_error")
            aliases = {normalized_name(label) for t in technicians for label in [t.workbook_name, *json.loads(t.aliases_json or "[]")]}
            candidates = sum(1 for x in db.scalars(select(DiscoveredTechnician).where(DiscoveredTechnician.ignored.is_(False)))
                             if x.normalized_name not in aliases)
        enabled = [t for t in technicians if t.enabled]
        oauth_kind, oauth_text, needs_reauth = oauth_ui_status()
        return render(request, "dashboard.html", complete=complete, cfg=cfg, runs=runs, last=runs[0] if runs else None,
                      technicians=technicians, enabled=len(enabled), onboarded=sum(1 for t in enabled if t.calendar_id),
                      unonboarded=[t for t in enabled if not t.calendar_id], errored=[t for t in technicians if t.last_error],
                      pending=pending, tracked=tracked, candidates=candidates, next_sync_at=next_sync_at,
                      next_backup_at=next_backup_at, worker_failure=worker_failure, oauth_kind=oauth_kind,
                      oauth_text=oauth_text, needs_reauth=needs_reauth,
                      worker_ready=complete and not worker_failure and not needs_reauth and not settings.validate_runtime())

    # ------------------------------------------------------------------ sync actions
    @app.post("/sync")
    def sync_now(request: Request, form: dict = Depends(read_form)):
        require_auth(request)
        require_csrf(request, form.get("csrf_token"))
        mode = form.get("mode", "")
        if mode not in {"dry-run", "apply"}:
            raise HTTPException(400, "Neplatný režim")
        with sessions() as db:
            if not setup_complete(db):
                raise HTTPException(409, "Setup není dokončen")
            runtime = load_runtime(db)
        prepare_service(runtime)
        try:
            result = service.sync(dry_run=mode == "dry-run")
            SYNC_COUNT.labels(mode, result["status"]).inc()
        except Exception as exc:
            SYNC_COUNT.labels(mode, "error").inc()
            log.exception("manual_sync_failed")
            return message_page(request, "Synchronizace selhala", f"{worker_error(exc)['message']} Podrobnosti jsou v logu a v historii běhů.",
                                "bad", 502, "/runs", "Historie běhů")
        if result.get("status") == "busy":
            flash(request, "warn", "Jiná synchronizace právě běží, zkuste to za chvíli.")
            return RedirectResponse("/", 303)
        return RedirectResponse(f"/runs/{result['run_id']}" if result.get("run_id") else "/runs", 303)

    @app.get("/runs", response_class=HTMLResponse)
    def runs_page(request: Request):
        require_auth(request)
        with sessions() as db:
            runs = load_runs(db, 100)
            cfg = load_runtime(db)
        return render(request, "runs.html", runs=runs, tz=cfg.timezone)

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_detail(run_id: int, request: Request):
        require_auth(request)
        with sessions() as db:
            run = db.get(SyncRun, run_id)
            if run is None:
                raise HTTPException(404, "Běh nenalezen")
            cfg = load_runtime(db)
        try:
            summary = json.loads(run.summary_json or "{}")
        except ValueError:
            summary = {}
        return render(request, "run_detail.html", run=run, summary=summary, tz=cfg.timezone)

    @app.get("/audit", response_class=HTMLResponse)
    def audit_page(request: Request):
        require_auth(request)
        with sessions() as db:
            entries = list(db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(200)))
            cfg = load_runtime(db)
        rows = []
        for entry in entries:
            try:
                details = json.loads(entry.details_json or "{}")
            except ValueError:
                details = {}
            rows.append({"entry": entry, "details": details})
        return render(request, "audit.html", rows=rows, tz=cfg.timezone)

    @app.get("/stale", response_class=HTMLResponse)
    def stale_page(request: Request):
        require_auth(request)
        with sessions() as db:
            cfg = load_runtime(db)
            rows = []
            query = (select(OwnedEvent, Technician).join(Technician, Technician.id == OwnedEvent.technician_id)
                     .where(OwnedEvent.missing_streak > 0).order_by(OwnedEvent.pending_stale.desc(), Technician.workbook_name))
            for owned, tech in db.execute(query):
                parts = owned.source_key.split("|")
                rows.append({"technician": tech.workbook_name, "pending": owned.pending_stale, "streak": owned.missing_streak,
                             "date": parts[4] if len(parts) > 5 else "", "token": parts[5] if len(parts) > 5 else owned.source_key})
        return render(request, "stale.html", rows=rows, delete_enabled=cfg.delete_enabled, policy=cfg.stale_policy)

    # ------------------------------------------------------------------ technicians
    @app.get("/technicians", response_class=HTMLResponse)
    def technicians_page(request: Request):
        require_auth(request)
        with sessions() as db:
            cfg = load_runtime(db)
            technicians = list(db.scalars(select(Technician).order_by(Technician.workbook_name)))
            discovered = list(db.scalars(select(DiscoveredTechnician).order_by(DiscoveredTechnician.display_name)))
            aliases = {normalized_name(label) for t in technicians for label in [t.workbook_name, *json.loads(t.aliases_json or "[]")]}
            candidates = [x for x in discovered if not x.ignored and x.normalized_name not in aliases]
            ignored = [x for x in discovered if x.ignored]
            owned_counts = dict(db.execute(select(OwnedEvent.technician_id, func.count()).group_by(OwnedEvent.technician_id)).all())
            complete = setup_complete(db)
        return render(request, "technicians.html", technicians=technicians, candidates=candidates, ignored=ignored,
                      owned_counts=owned_counts, cfg=cfg, complete=complete,
                      calendar_names={t.id: calendar_name(cfg, t.workbook_name) for t in technicians},
                      awaiting=sum(1 for t in technicians if t.enabled and t.email and not t.calendar_id))

    def valid_email(value: str) -> bool:
        return bool(EMAIL_RE.match(value.strip()))

    @app.post("/technicians")
    def add_technician(request: Request, form: dict = Depends(read_form)):
        require_auth(request)
        require_csrf(request, form.get("csrf_token"))
        name, email = form.get("workbook_name", "").strip(), form.get("email", "").strip()
        if not name or not valid_email(email):
            flash(request, "bad", "Vyplňte jméno a platný e-mail.")
            return RedirectResponse("/technicians", 303)
        with sessions() as db:
            if db.scalar(select(Technician.id).where(Technician.workbook_name == name)):
                flash(request, "bad", "Technik s tímto jménem už existuje.")
                return RedirectResponse("/technicians", 303)
            db.add(Technician(workbook_name=name, email=email, enabled="enabled" in form))
            audit(db, "technician_added", name=name)
            db.commit()
        flash(request, "ok", f"Technik {name} byl přidán.")
        return RedirectResponse("/technicians", 303)

    @app.post("/discovered/{discovered_id}/add")
    def add_discovered(discovered_id: int, request: Request, form: dict = Depends(read_form)):
        require_auth(request)
        require_csrf(request, form.get("csrf_token"))
        email = form.get("email", "").strip().casefold()
        if not valid_email(email):
            flash(request, "bad", "Zadejte platný e-mail technika.")
            return RedirectResponse("/technicians", 303)
        with sessions() as db:
            item = db.get(DiscoveredTechnician, discovered_id)
            if not item or item.ignored:
                raise HTTPException(404)
            tech = db.scalar(select(Technician).where(Technician.workbook_name == item.display_name))
            if not tech:
                canonical = "discovered-" + hashlib.sha256(item.normalized_name.encode()).hexdigest()[:16]
                tech = Technician(canonical_id=canonical, workbook_name=item.display_name)
                db.add(tech)
            tech.email = email
            tech.enabled = True
            audit(db, "discovered_technician_added", name=item.display_name)
            schedule_resync(db)
            db.commit()
        flash(request, "ok", f"Technik {item.display_name} byl přidán.")
        return RedirectResponse("/technicians", 303)

    @app.post("/discovered/{discovered_id}/ignore")
    def ignore_discovered(discovered_id: int, request: Request, form: dict = Depends(read_form)):
        require_auth(request)
        require_csrf(request, form.get("csrf_token"))
        with sessions() as db:
            item = db.get(DiscoveredTechnician, discovered_id)
            if not item:
                raise HTTPException(404)
            item.ignored = True
            db.commit()
        return RedirectResponse("/technicians", 303)

    @app.post("/discovered/{discovered_id}/restore")
    def restore_discovered(discovered_id: int, request: Request, form: dict = Depends(read_form)):
        require_auth(request)
        require_csrf(request, form.get("csrf_token"))
        with sessions() as db:
            item = db.get(DiscoveredTechnician, discovered_id)
            if not item:
                raise HTTPException(404)
            item.ignored = False
            db.commit()
        return RedirectResponse("/technicians", 303)

    @app.post("/technicians/{technician_id}/update")
    def update_technician(technician_id: int, request: Request, form: dict = Depends(read_form)):
        require_auth(request)
        require_csrf(request, form.get("csrf_token"))
        email = form.get("email", "").strip().casefold()
        if not valid_email(email):
            flash(request, "bad", "Zadejte platný e-mail.")
            return RedirectResponse("/technicians", 303)
        with sessions() as db:
            tech = db.get(Technician, technician_id)
            if not tech:
                raise HTTPException(404)
            tech.email = email
            tech.calendar_id = form.get("calendar_id", "").strip() or None
            tech.enabled = "enabled" in form
            audit(db, "technician_changed", id=technician_id, name=tech.workbook_name, enabled=tech.enabled)
            schedule_resync(db)
            db.commit()
        flash(request, "ok", "Změny byly uloženy.")
        return RedirectResponse("/technicians", 303)

    @app.post("/technicians/{technician_id}/remove")
    def remove_technician(technician_id: int, request: Request, form: dict = Depends(read_form)):
        require_auth(request)
        require_csrf(request, form.get("csrf_token"))
        with sessions() as db:
            tech = db.get(Technician, technician_id)
            if not tech:
                raise HTTPException(404)
            owned = list(db.scalars(select(OwnedEvent).where(OwnedEvent.technician_id == technician_id)))
            if (tech.calendar_id or owned) and form.get("confirm") != "yes":
                raise HTTPException(409, "Je nutné výslovně potvrdit odstranění pouze lokálního mapování")
            for event in owned:
                db.delete(event)
            audit(db, "technician_mapping_removed", id=technician_id, calendar_preserved=bool(tech.calendar_id),
                  owned_event_rows_removed=len(owned))
            db.delete(tech)
            schedule_resync(db)
            db.commit()
        flash(request, "ok", "Technik byl odebrán z aplikace. Google kalendář zůstal beze změny.")
        return RedirectResponse("/technicians", 303)

    def onboard_one(db, tech: Technician, cfg: RuntimeConfig) -> tuple[bool, str, bool]:
        """Create/reuse the calendar and grant read-only ACL. Returns (ok, message, invitation_sent)."""
        if not tech.email:
            return False, "Technik nemá e-mail.", False
        try:
            result = google.onboard_with_prefix(tech.workbook_name, tech.email, cfg.calendar_prefix, tech.calendar_id,
                                                cfg.timezone, calendar_name(cfg, tech.workbook_name))
            if isinstance(result, tuple):
                calendar_id, link = result
                sent = True
            else:
                calendar_id, link, sent = result.calendar_id, result.subscribe_link, result.invitation_sent
        except TypeError:  # legacy backend without the ``summary`` keyword
            result = google.onboard_with_prefix(tech.workbook_name, tech.email, cfg.calendar_prefix, tech.calendar_id, cfg.timezone)
            if isinstance(result, tuple):
                calendar_id, link = result
                sent = True
            else:
                calendar_id, link, sent = result.calendar_id, result.subscribe_link, result.invitation_sent
        except Exception as exc:  # noqa: BLE001 - Google SDK errors are heterogeneous
            tech.onboarding_status = "failed"
            tech.last_error = type(exc).__name__
            log.warning("onboarding_failed", extra={"technician": tech.workbook_name, "error": type(exc).__name__})
            return False, "Kalendář nebo oprávnění se nepodařilo nastavit.", False
        tech.calendar_id, tech.onboarding_link = calendar_id, link
        tech.onboarding_status, tech.last_error = "shared", None
        audit(db, "technician_onboarded", name=tech.workbook_name, invitation_sent=sent)
        return True, "", sent

    @app.post("/technicians/{technician_id}/onboard")
    def onboard(technician_id: int, request: Request, form: dict = Depends(read_form)):
        require_auth(request)
        require_csrf(request, form.get("csrf_token"))
        with sessions() as db:
            tech = db.get(Technician, technician_id)
            if not tech:
                raise HTTPException(404)
            if not tech.email:
                raise HTTPException(409, "Technik bez e-mailu nelze onboardovat")
            cfg = load_runtime(db)
            ok, error, sent = onboard_one(db, tech, cfg)
            if not ok:
                db.commit()
                return message_page(request, "Onboarding selhal", error + " Nic lokálního nebylo odstraněno; "
                                    "zkuste akci znovu nebo zkontrolujte Google oprávnění.", "bad", 502, "/technicians", "Zpět")
            schedule_resync(db)
            db.commit()
            name, link = calendar_name(cfg, tech.workbook_name), tech.onboarding_link
        message = ("Jednorázová pozvánka ke sdílenému kalendáři byla odeslána." if sent else
                   "Přístup ke čtení už existoval; Jednorázová pozvánka ke sdílenému kalendáři nebyla znovu odeslána.")
        return render(request, "onboarded.html", message=message, calendar=name, link=link)

    @app.post("/technicians/onboard-all")
    def onboard_all(request: Request, form: dict = Depends(read_form)):
        require_auth(request)
        require_csrf(request, form.get("csrf_token"))
        done = failed = invited = 0
        with sessions() as db:
            cfg = load_runtime(db)
            for tech in db.scalars(select(Technician).where(Technician.enabled.is_(True), Technician.calendar_id.is_(None))):
                if not tech.email:
                    continue
                ok, _, sent = onboard_one(db, tech, cfg)
                done += ok
                failed += not ok
                invited += bool(ok and sent)
                db.commit()
            if done:
                schedule_resync(db)
                db.commit()
        flash(request, "bad" if failed else "ok", f"Onboarding: {done} hotovo ({invited} pozvánek odesláno), {failed} selhalo.")
        return RedirectResponse("/technicians", 303)

    # ------------------------------------------------------------------ settings & wizard
    def checked(form: dict, key: str) -> bool:
        return key in form

    def config_from_form(form: dict, base: RuntimeConfig) -> tuple[RuntimeConfig, list[str]]:
        cfg = dataclasses.replace(base)
        cfg.drive_file_id = extract_drive_id(form.get("drive_file", base.drive_file_id))
        cfg.organization = form.get("organization", base.organization).strip()
        cfg.calendar_template = form.get("calendar_template", base.calendar_template).strip()
        cfg.calendar_prefix = form.get("calendar_prefix", base.calendar_prefix)
        cfg.timezone = form.get("timezone", base.timezone).strip()
        cfg.schedule_mode = form.get("schedule_mode", base.schedule_mode)
        cfg.interval_minutes = _int(form, "interval_minutes", base.interval_minutes)
        cfg.daily_times = [x.strip() for x in re.split(r"[,\s;]+", form.get("daily_times", "")) if x.strip()] or base.daily_times
        cfg.stability_seconds = _int(form, "stability_seconds", base.stability_seconds)
        enabled_backup = checked(form, "backup_enabled")
        cfg.backup_frequency = form.get("backup_frequency", "daily") if enabled_backup else "disabled"
        cfg.backup_time = form.get("backup_time", base.backup_time)
        cfg.backup_retention_count = _int(form, "backup_retention_count", base.backup_retention_count)
        cfg.backup_retention_days = _int(form, "backup_retention_days", base.backup_retention_days)
        cfg.backup_directory = form.get("backup_directory", base.backup_directory).strip()
        cfg.delete_enabled = checked(form, "delete_enabled")
        cfg.deployment_name = form.get("deployment_name", base.deployment_name).strip()
        cfg.sheet_pattern = form.get("sheet_pattern", base.sheet_pattern).strip() or "*"
        cfg.technician_name_column = form.get("technician_name_column", base.technician_name_column).strip().upper()
        cfg.header_scan_rows = _int(form, "header_scan_rows", base.header_scan_rows)
        cfg.first_data_column = form.get("first_data_column", "").strip().upper()
        cfg.sync_horizon_days = _int(form, "sync_horizon_days", base.sync_horizon_days)
        cfg.sync_lookback_days = _int(form, "sync_lookback_days", base.sync_lookback_days)
        cfg.auto_detect_dates = checked(form, "auto_detect_dates")
        cfg.continuation_policy = form.get("continuation_policy", base.continuation_policy)
        cfg.stale_policy = form.get("stale_policy", base.stale_policy)
        cfg.assignment_drop_threshold = _float(form, "assignment_drop_threshold", base.assignment_drop_threshold)
        cfg.parse_phones = checked(form, "parse_phones")
        cfg.parse_addresses = checked(form, "parse_addresses")
        errors: list[str] = []
        try:
            colors = json.loads(form.get("color_map_json", "") or "{}")
            if not isinstance(colors, dict):
                raise TypeError("color map must be an object")
            cfg.color_map = {str(k).upper(): str(v) for k, v in colors.items()}
        except (ValueError, TypeError):
            errors.append("Barvy musí být JSON objekt ve tvaru {\"TMB\": \"3\"}")
        errors.extend(cfg.validate())
        return cfg, errors

    def settings_context(request: Request, cfg: RuntimeConfig | None = None, errors: list[str] | None = None) -> dict:
        with sessions() as db:
            stored = load_runtime(db)
            complete = setup_complete(db)
            next_sync_at = get_setting(db, "next_sync_at")
            next_backup_at = get_setting(db, "next_backup_at")
        oauth_kind, oauth_text, needs_reauth = oauth_ui_status()
        redirect_uri, direct = oauth_redirect_uri()
        return {"cfg": cfg or stored, "errors": errors or [], "complete": complete, "next_sync_at": next_sync_at,
                "next_backup_at": next_backup_at, "oauth_kind": oauth_kind, "oauth_text": oauth_text,
                "needs_reauth": needs_reauth, "oauth_direct": direct, "oauth_redirect": redirect_uri,
                "color_json": json.dumps((cfg or stored).color_map, ensure_ascii=False), "has_client_file": client_status()["present"], "client": client_status()}

    async def read_client_upload(request: Request) -> dict:
        form = await request.form()
        text = str(form.get("client_json") or "").strip()
        upload = form.get("client_file")
        if not text and upload is not None and hasattr(upload, "read"):
            raw = await upload.read(21_000)
            text = raw.decode("utf-8", errors="replace").strip()
        return {"csrf_token": str(form.get("csrf_token") or ""), "text": text}

    @app.post("/settings/oauth-client")
    def save_oauth_client(request: Request, data: dict = Depends(read_client_upload)):
        require_auth(request)
        require_csrf(request, data["csrf_token"])
        back = "/settings"
        if not data["text"]:
            flash(request, "bad", "Vložte obsah JSON nebo vyberte soubor.")
            return RedirectResponse(back, 303)
        try:
            target = settings.oauth_client_file
            try:
                document = install_client_file(data["text"], target)
            except PermissionError:  # read-only secret mount (Docker): keep it in the persistent data volume
                target = uploaded_client_file
                document = install_client_file(data["text"], target)
            except OSError:
                target = uploaded_client_file
                document = install_client_file(data["text"], target)
        except ClientConfigError as exc:
            flash(request, "bad", str(exc))
            return RedirectResponse(back, 303)
        if isinstance(google, GoogleClient):
            google.client_file = target
            google._credentials = None
        with sessions() as db:
            audit(db, "oauth_client_uploaded", client=client_id_hint(document))  # never the secret
            db.commit()
        flash(request, "ok", "OAuth klient byl uložen. Nyní klikněte na „Autorizovat Google“.")
        return RedirectResponse(back, 303)

    @app.post("/settings/password")
    def change_password(request: Request, form: dict = Depends(read_form)):
        require_auth(request)
        require_csrf(request, form.get("csrf_token"))
        client = request.client.host if request.client else "unknown"
        if throttle.blocked(client):
            flash(request, "bad", "Příliš mnoho neúspěšných pokusů. Zkuste to za několik minut.")
            return RedirectResponse("/settings", 303)
        current, new, confirm = form.get("current_password", ""), form.get("new_password", ""), form.get("confirm_password", "")
        with sessions() as db:
            valid = verify_admin_password(db, current, settings.admin_password)
        if not valid:
            throttle.fail(client)
            flash(request, "bad", "Současné heslo není správné.")
            return RedirectResponse("/settings", 303)
        problems = password_problems(new, confirm, current)
        if problems:
            flash(request, "bad", " ".join(problems))
            return RedirectResponse("/settings", 303)
        with sessions() as db:
            set_admin_password(db, new)
            db.commit()
        request.session.clear()  # every session must sign in again with the new password
        flash(request, "ok", "Heslo bylo změněno. Přihlaste se novým heslem.")
        return RedirectResponse("/login", 303)

    @app.get("/help", response_class=HTMLResponse)
    def help_page(request: Request):
        require_auth(request)
        return render(request, "help.html", has_client_file=client_status()["present"], client=client_status())

    @app.get("/help/google")
    def google_help(request: Request):
        require_auth(request)
        return RedirectResponse("/help#google", 303)

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        require_auth(request)
        return render(request, "settings.html", **settings_context(request))

    @app.post("/settings/save")
    def save_settings(request: Request, form: dict = Depends(read_form)):
        require_auth(request)
        require_csrf(request, form.get("csrf_token"))
        with sessions() as db:
            before = load_runtime(db)
            complete = setup_complete(db)
        cfg, errors = config_from_form(form, before)
        if cfg.delete_enabled and not before.delete_enabled and not checked(form, "confirm_delete"):
            errors.append("Zapnutí mazání vyžaduje zaškrtnutí potvrzení pod přepínačem.")
        if errors:
            return render(request, "settings.html", 400, **settings_context(request, cfg, errors))
        changed = material_changes(before, cfg)
        with sessions() as db:
            save_runtime(db, cfg)
            if changed and complete:
                for key in ("connection_test_ok", "mapping_test_ok", "dry_run_ok"):
                    set_setting(db, key, False)
                set_setting(db, "setup_statuses", {})
                set_setting(db, "setup_complete", False)
                set_setting(db, "next_sync_at", None)
                set_setting(db, "next_backup_at", None)
            elif complete:
                store_next_times(db, cfg, dt.datetime.now(dt.UTC))
            elif changed:
                for key in ("connection_test_ok", "mapping_test_ok", "dry_run_ok"):
                    set_setting(db, key, False)
            db.commit()
        if changed and complete:
            flash(request, "warn", "Změnilo se: " + ", ".join(changed) + ". Synchronizace je pozastavena, dokud nepoběží nové ověření v průvodci.")
            return RedirectResponse("/setup", 303)
        flash(request, "ok", "Nastavení bylo uloženo.")
        return RedirectResponse("/settings", 303)

    def mapping_preview(path: Path, cfg: RuntimeConfig) -> dict:
        with sessions() as db:
            technicians = list(db.scalars(select(Technician)))
        identities = [TechnicianIdentity(x.canonical_id or f"legacy-{x.id}", x.workbook_name, tuple(json.loads(x.aliases_json or "[]"))) for x in technicians]
        parser_cfg = ParserConfig(workbook_profile=cfg.deployment_name, sheet_pattern=cfg.sheet_pattern,
                                  technician_name_column=cfg.technician_name_column, header_scan_rows=cfg.header_scan_rows,
                                  first_data_column=cfg.first_data_column or None, auto_detect_dates=cfg.auto_detect_dates,
                                  continuation_policy=cfg.continuation_policy, color_map=cfg.color_map,
                                  parse_phones=cfg.parse_phones, parse_addresses=cfg.parse_addresses)
        today = dt.datetime.now(dt.UTC).astimezone(ZoneInfo(cfg.timezone)).date()
        events, issues = parse_workbook(path, cfg.drive_file_id or "preview", "preview", "preview", identities, cfg.timezone, parser_cfg,
                                        today - dt.timedelta(days=cfg.sync_lookback_days), today + dt.timedelta(days=cfg.sync_horizon_days))
        with sessions() as db:
            record_discovered(db, issues)
        issue_counts: dict[str, int] = {}
        for issue in issues:
            code = str(issue.get("code", "unknown"))
            issue_counts[code] = issue_counts.get(code, 0) + 1
        return {"event_count": len(events),
                "sample": [{"technician": x.technician, "summary": x.summary[:120], "start": x.start} for x in events[:20]],
                "issue_count": len(issues), "issue_counts": issue_counts, "issues": issues[:50], "issues_truncated": len(issues) > 50,
                "suggestion": {"technician_name_column": cfg.technician_name_column, "sheet_pattern": cfg.sheet_pattern,
                               "date_headers": "auto" if cfg.auto_detect_dates else cfg.first_data_column}}

    def save_setup_step(key: str, status: str, detail: str) -> None:
        with sessions() as db:
            statuses = get_setting(db, "setup_statuses", {}) or {}
            statuses[key] = {"status": status, "detail": detail}
            set_setting(db, "setup_statuses", statuses)
            db.commit()

    def wizard_context(request: Request, error: str = "", failed_step: str = "") -> dict:
        with sessions() as db:
            cfg = load_runtime(db)
            complete = setup_complete(db)
            statuses = get_setting(db, "setup_statuses", {}) or {}
            mapping_ok = bool(get_setting(db, "mapping_test_ok", False))
            preview = get_setting(db, "mapping_preview", {}) or {}
            enabled = list(db.scalars(select(Technician).where(Technician.enabled.is_(True))))
            discovered = db.scalar(select(func.count()).select_from(DiscoveredTechnician)) or 0
            next_sync_at = get_setting(db, "next_sync_at")
            next_backup_at = get_setting(db, "next_backup_at")
            dry = get_setting(db, "setup_dry_run_summary")
        _, oauth_text, needs_reauth = oauth_ui_status()
        onboarded = sum(bool(t.calendar_id) for t in enabled)
        steps = [
            {"key": "oauth", "title": "Google účet (OAuth)", "ok": not needs_reauth, "detail": oauth_text},
            {"key": "validation", "title": "Zdrojový soubor na Google Drive", "ok": bool(cfg.drive_file_id),
             "detail": cfg.drive_file_id or "není zadán"},
            {"key": "mapping", "title": "Načtení techniků z XLSX", "ok": mapping_ok,
             "detail": f"nalezeno událostí: {preview.get('event_count', 0)}, jmen: {discovered}" if preview else "zatím neproběhlo"},
            {"key": "technicians", "title": "Technici s e-maily", "ok": bool(enabled), "detail": f"{len(enabled)} aktivních"},
            {"key": "dry_run", "title": "Kontrolní dry-run (nic se nezapisuje)", "ok": bool(dry) and complete, "detail": ""},
            {"key": "activation", "title": "Aktivace workeru", "ok": complete, "detail": ""},
            {"key": "onboarding", "title": "Onboarding kalendářů", "ok": bool(enabled) and onboarded == len(enabled),
             "detail": f"{onboarded}/{len(enabled)}"},
        ]
        for step in steps:
            status = statuses.get(step["key"], {})
            step["failed"] = failed_step == step["key"] or status.get("status") == "failed"
            if status.get("detail") and step["failed"]:
                step["detail"] = status["detail"]
        return {"cfg": cfg, "complete": complete, "steps": steps, "error": error, "failed_step": failed_step,
                "next_sync_at": next_sync_at, "next_backup_at": next_backup_at, "needs_reauth": needs_reauth,
                "enabled_count": len(enabled), "preview": preview, "has_client_file": client_status()["present"], "client": client_status()}

    @app.get("/setup", response_class=HTMLResponse)
    def setup_page(request: Request):
        require_auth(request)
        return render(request, "setup.html", **wizard_context(request))

    @app.post("/setup/source")
    def setup_source(request: Request, form: dict = Depends(read_form)):
        require_auth(request)
        require_csrf(request, form.get("csrf_token"))
        file_id = extract_drive_id(form.get("drive_file", ""))
        if not file_id:
            flash(request, "bad", "Zadejte ID nebo URL souboru na Google Drive.")
            return RedirectResponse("/setup", 303)
        with sessions() as db:
            cfg = load_runtime(db)
            if cfg.drive_file_id != file_id:
                cfg.drive_file_id = file_id
                save_runtime(db, cfg)
                for key in ("connection_test_ok", "mapping_test_ok", "dry_run_ok"):
                    set_setting(db, key, False)
                set_setting(db, "setup_complete", False)
                set_setting(db, "setup_statuses", {})
            db.commit()
        flash(request, "ok", "Zdrojový soubor byl uložen.")
        return RedirectResponse("/setup", 303)

    @app.post("/setup/scan")
    def setup_scan(request: Request, form: dict = Depends(read_form)):
        """Download the XLSX read-only, validate the layout and remember the technician names found."""
        require_auth(request)
        require_csrf(request, form.get("csrf_token"))
        with sessions() as db:
            cfg = load_runtime(db)
        if not cfg.drive_file_id:
            flash(request, "bad", "Nejdřív zadejte zdrojový soubor.")
            return RedirectResponse("/setup", 303)
        try:
            check_access(cfg.drive_file_id)
            with tempfile.TemporaryDirectory(dir=settings.data_dir) as work:
                path = Path(work) / "mapping.xlsx"
                google.download(cfg.drive_file_id, path)
                preview = mapping_preview(path, cfg)
        except Exception as exc:  # noqa: BLE001
            log.warning("setup_scan_failed", extra={"error": type(exc).__name__})
            save_setup_step("mapping", "failed", f"Stažení nebo čtení XLSX selhalo ({type(exc).__name__}).")
            flash(request, "bad", f"Soubor se nepodařilo načíst ({type(exc).__name__}). Zkontrolujte ID souboru a sdílení účtu.")
            return RedirectResponse("/setup", 303)
        fatal = [x for x in preview["issues"] if x.get("severity") == "fatal"]
        with sessions() as db:
            set_setting(db, "mapping_preview", preview)
            set_setting(db, "mapping_test_ok", not fatal)
            db.commit()
        if fatal:
            codes = ", ".join(str(x.get("code", "?")) for x in fatal)
            save_setup_step("mapping", "failed", f"Fatální problém v mapování: {codes}")
            flash(request, "bad", f"Mapování XLSX selhalo: {codes}. Upravte pokročilé nastavení parseru.")
        else:
            save_setup_step("mapping", "ok", f"Nalezeno událostí: {preview['event_count']}")
            flash(request, "ok", "XLSX byl načten. Doplňte e-maily nalezeným technikům.")
        return RedirectResponse("/setup", 303)

    def activation_failure(request: Request, step: str, detail: str, status_code: int = 400) -> HTMLResponse:
        save_setup_step(step, "failed", detail)
        return render(request, "setup.html", status_code, **wizard_context(request, error=detail, failed_step=step))

    @app.post("/setup/verify-and-activate")
    def verify_and_activate(request: Request, form: dict = Depends(read_form)):
        require_auth(request)
        require_csrf(request, form.get("csrf_token"))
        now = dt.datetime.now(dt.UTC)
        with sessions() as db:
            cfg = load_runtime(db)
            enabled_count = len(list(db.scalars(select(Technician).where(Technician.enabled.is_(True)))))
            for key in ("setup_complete", "next_sync_at", "next_backup_at"):
                set_setting(db, key, False if key == "setup_complete" else None)
            set_setting(db, "setup_statuses", {})
            db.commit()
        errors = cfg.validate()
        if errors:
            return activation_failure(request, "validation", "; ".join(errors))
        save_setup_step("validation", "ok", "Nastavení je platné")
        if not has_token():
            return activation_failure(request, "oauth", "Chybí Google OAuth token; dokončete autorizaci.")
        save_setup_step("oauth", "ok", "OAuth token je dostupný")
        try:
            name = check_access(cfg.drive_file_id)
        except Exception as exc:  # noqa: BLE001 - Google SDK has heterogeneous errors
            return activation_failure(request, "connection", f"Test Drive/Calendar selhal ({type(exc).__name__}).", 502)
        with sessions() as db:
            set_setting(db, "connection_test_ok", True)
            db.commit()
        save_setup_step("connection", "ok", f"Soubor: {name}")
        try:
            with tempfile.TemporaryDirectory(dir=settings.data_dir) as work:
                path = Path(work) / "mapping.xlsx"
                google.download(cfg.drive_file_id, path)
                preview = mapping_preview(path, cfg)
        except Exception as exc:  # noqa: BLE001 - render a wizard failure at this boundary
            return activation_failure(request, "mapping", f"Stažení nebo mapování XLSX selhalo ({type(exc).__name__}).", 502)
        fatal = [x for x in preview["issues"] if x.get("severity") == "fatal"]
        with sessions() as db:
            set_setting(db, "mapping_preview", preview)
            set_setting(db, "mapping_test_ok", not fatal)
            db.commit()
        if fatal:
            codes = ", ".join(str(x.get("code", "neznámá chyba")) for x in fatal)
            return activation_failure(request, "mapping", f"Mapování obsahuje fatální chybu: {codes}.")
        save_setup_step("mapping", "ok", f"Nalezeno událostí: {preview['event_count']}")
        if not enabled_count:
            return activation_failure(request, "technicians", "Není povolen žádný technik; přiřaďte e-mail nalezenému jménu na stránce Technici.")
        save_setup_step("technicians", "ok", f"{enabled_count} aktivních")
        prepare_service(cfg, allow_delete=False)
        try:
            result = service.sync(dry_run=True)
        except Exception as exc:  # noqa: BLE001 - render a wizard failure at this boundary
            return activation_failure(request, "dry_run", f"Dry-run selhal ({type(exc).__name__}).", 502)
        if result.get("status") != "ok":
            return activation_failure(request, "dry_run", f"Dry-run skončil stavem {result.get('status', 'neznámý')}.")
        with sessions() as db:
            set_setting(db, "dry_run_ok", True)
            set_setting(db, "setup_dry_run_summary", result)
            set_setting(db, "setup_complete", True)
            store_next_times(db, cfg, now)
            audit(db, "setup_activated")
            db.commit()
        save_setup_step("dry_run", "ok", "Dry-run bez mazání proběhl úspěšně")
        save_setup_step("activation", "ok", "Worker je aktivní")
        flash(request, "ok", "Synchronizace je aktivní. Nezapomeňte provést onboarding kalendářů techniků.")
        return RedirectResponse("/setup", 303)

    @app.get("/setup/workbook-mapping", response_class=HTMLResponse)
    def workbook_mapping(request: Request):
        require_auth(request)
        with sessions() as db:
            cfg = load_runtime(db)
            preview = get_setting(db, "mapping_preview", {})
            ok = get_setting(db, "mapping_test_ok", False)
        return render(request, "mapping.html", cfg=cfg, preview=preview, ok=ok, oauth_ready=has_token())

    @app.post("/setup/workbook-mapping/test")
    def workbook_mapping_test(request: Request, form: dict = Depends(read_form)):
        require_auth(request)
        require_csrf(request, form.get("csrf_token"))
        with sessions() as db:
            cfg = load_runtime(db)
        try:
            with tempfile.TemporaryDirectory(dir=settings.data_dir) as work:
                path = Path(work) / "mapping.xlsx"
                google.download(cfg.drive_file_id, path)
                preview = mapping_preview(path, cfg)
        except Exception as exc:  # noqa: BLE001
            flash(request, "bad", f"Soubor se nepodařilo načíst ({type(exc).__name__}).")
            return RedirectResponse("/setup/workbook-mapping", 303)
        ok = not any(x.get("severity") == "fatal" for x in preview["issues"])
        with sessions() as db:
            set_setting(db, "mapping_preview", preview)
            set_setting(db, "mapping_test_ok", ok)
            db.commit()
        return RedirectResponse("/setup/workbook-mapping", 303)

    return app
