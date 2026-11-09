from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import sys

from .config import Settings
from .google import GoogleClient, OAuthReauthorizationRequired
from .models import init_db, make_session_factory
from .service import SyncService
from .setup import get_setting, load_runtime, setup_complete


def configure_logging(level: str) -> None:
    class JsonFormatter(logging.Formatter):
        def format(self, record):
            return json.dumps({"time": self.formatTime(record, "%Y-%m-%dT%H:%M:%SZ"), "level": record.levelname,
                               "logger": record.name, "message": record.getMessage()}, ensure_ascii=False)
    handler = logging.StreamHandler(); handler.setFormatter(JsonFormatter())
    root = logging.getLogger(); root.handlers[:] = [handler]; root.setLevel(level)


def serve(args) -> None:
    """Run the web UI + embedded scheduler in this process (local use; Docker uses uvicorn directly)."""
    import uvicorn

    host = args.host or "0.0.0.0"
    uvicorn.run("calendar_sync.web:create_app", factory=True, host=host, port=args.port or 8080,
                proxy_headers=False, log_level="info")


def set_password_command(sessions) -> None:
    """Set the admin password: interactive (twice) on a terminal, or one line on stdin when piped."""
    import getpass

    from .auth import password_problems
    from .setup import set_admin_password
    if sys.stdin.isatty():
        new = getpass.getpass("Nové heslo správce: ")
        confirm = getpass.getpass("Nové heslo znovu: ")
    else:
        new = sys.stdin.readline().rstrip("\r\n")
        confirm = new
    problems = password_problems(new, confirm)
    if problems:
        print(" ".join(problems), file=sys.stderr)
        sys.exit(2)
    with sessions() as db:
        set_admin_password(db, new, "cli")
        db.commit()
    print("Heslo správce bylo nastaveno.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["serve", "set-password", "init-db", "oauth-bootstrap", "dry-run", "sync-now", "status"])
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()
    if args.command in {"serve"}:
        serve(args)
        return
    settings = Settings.from_env(); configure_logging(settings.log_level)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    engine, sessions = make_session_factory(settings.database_url); init_db(engine)
    if args.command == "set-password":
        set_password_command(sessions)
        return
    google = GoogleClient(settings.oauth_client_file, settings.oauth_token_file)
    if args.command == "init-db": print("database initialized"); return
    if args.command == "oauth-bootstrap": google.bootstrap(); print("OAuth token stored securely"); return
    if args.command == "status":
        errors = settings.validate_runtime()
        token_present = settings.oauth_token_file.exists()
        oauth_usable = False
        if not token_present:
            errors.append("OAuth token missing")
        else:
            try:
                google.credentials()
                oauth_usable = True
            except OAuthReauthorizationRequired:
                errors.append("Google OAuth reauthorization required")
            except Exception as exc:  # noqa: BLE001 - sanitized status boundary
                errors.append(f"OAuth credentials unusable: {type(exc).__name__}")
        with sessions() as db:
            configured = setup_complete(db)
            worker_error = get_setting(db, "worker_last_error")
        if not configured:
            errors.append("First-run setup incomplete")
        if worker_error:
            errors.append(str(worker_error.get("message", "Latest worker run failed")))
        print(json.dumps({"ready": not errors, "token_present": token_present,
                          "oauth_usable": oauth_usable, "worker_last_error": worker_error,
                          "errors": errors}, ensure_ascii=False))
        return
    # Runtime settings (Drive file, timezone, delete policy) live in the database, not in the environment.
    with sessions() as db:
        runtime = load_runtime(db)
    if not runtime.drive_file_id:
        print("Zdrojový soubor není nastaven (Průvodce v aplikaci).", file=sys.stderr); sys.exit(2)
    settings = dataclasses.replace(settings, source_file_id=runtime.drive_file_id, stability_seconds=runtime.stability_seconds,
                                   delete_enabled=runtime.delete_enabled and args.command == "sync-now")
    service = SyncService(settings, sessions, google)
    service.timezone = runtime.timezone
    result = service.sync(dry_run=args.command == "dry-run")
    print(json.dumps(result, ensure_ascii=False, indent=2)); sys.exit(0 if result["status"] in {"ok", "deferred"} else 1)


if __name__ == "__main__": main()
