from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from .platform import file_lock, harden_fd, harden_path, keep_owner

RETRIES = 4  # exponential back-off on 5xx / rate limit handled by googleapiclient
SCOPES = ["https://www.googleapis.com/auth/drive.readonly", "https://www.googleapis.com/auth/calendar"]


class OAuthReauthorizationRequired(RuntimeError):
    """The stored grant can no longer be refreshed and must be replaced."""


class OAuthTransientError(RuntimeError):
    """A sanitized, retryable OAuth provider or transport failure."""


@dataclass(frozen=True)
class DriveMeta:
    id: str
    name: str
    version: str
    md5: str
    modified_time: str
    size: str

    @property
    def fingerprint(self) -> str:
        return f"{self.version}:{self.md5}:{self.size}"


@dataclass(frozen=True)
class OnboardResult:
    calendar_id: str
    subscribe_link: str
    invitation_sent: bool
    acl_already_present: bool


class GoogleClient:
    def __init__(self, client_file: Path, token_file: Path):
        self.client_file = client_file
        self.token_file = token_file
        self._credentials: Credentials | None = None
        self._pending_flows: dict[str, InstalledAppFlow] = {}
        self._thread_lock = threading.RLock()

    @contextmanager
    def _token_lock(self) -> Iterator[None]:
        """Serialize token read/refresh/write across threads and processes."""
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.token_file.with_name(f".{self.token_file.name}.lock")
        with self._thread_lock, lock_path.open("a+b") as handle:
            harden_fd(handle.fileno())
            with file_lock(handle):
                yield

    def credentials(self) -> Credentials:
        with self._token_lock():
            if self._credentials and self._credentials.valid:
                return self._credentials
            if not self.token_file.exists():
                raise RuntimeError("OAuth token missing; run `calendar-sync oauth-bootstrap`")
            # Reload while holding the process lock. Another worker may have
            # refreshed or rotated the grant before this process acquired it.
            creds = Credentials.from_authorized_user_file(str(self.token_file), SCOPES)
            if creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except RefreshError as exc:
                    self._credentials = None
                    if "invalid_grant" in str(exc).casefold():
                        raise OAuthReauthorizationRequired(
                            "Google OAuth authorization must be renewed"
                        ) from None
                    raise OAuthTransientError("Google OAuth refresh temporarily failed") from None
                self._write_token_unlocked(creds)
            if not creds.valid:
                raise RuntimeError("OAuth credentials are invalid")
            self._credentials = creds
            return creds

    def bootstrap(self) -> None:
        client_config = json.loads(self.client_file.read_text(encoding="utf-8"))
        client_kind = "installed" if "installed" in client_config else "web"
        client_config[client_kind]["auth_uri"] = "https://accounts.google.com/o/oauth2/v2/auth"
        flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
        creds = flow.run_local_server(
            host="localhost",
            bind_addr="0.0.0.0",
            port=8081,
            open_browser=False,
            access_type="offline",
            prompt="consent",
        )
        self._write_token(creds, require_refresh=True)

    def authorization_url(self, redirect_uri: str, state: str) -> str:
        flow = self._web_flow(redirect_uri, state)
        url, _ = flow.authorization_url(
            access_type="offline", prompt="consent", include_granted_scopes="true"
        )
        self._pending_flows[state] = flow
        return url

    def complete_authorization(self, redirect_uri: str, state: str, code: str) -> None:
        flow = self._pending_flows.pop(state, None)
        if flow is None or flow.redirect_uri != redirect_uri:
            raise RuntimeError("OAuth authorization expired; start it again")
        try:
            flow.fetch_token(code=code)
        except Exception:  # noqa: BLE001 - provider SDK exceptions are heterogeneous and may contain secrets
            raise OAuthTransientError("Google OAuth code exchange failed") from None
        self._write_token(flow.credentials, require_refresh=True)
        self._credentials = flow.credentials

    def _web_flow(self, redirect_uri: str, state: str) -> InstalledAppFlow:
        client_config = json.loads(self.client_file.read_text(encoding="utf-8"))
        client_kind = "installed" if "installed" in client_config else "web"
        client_config[client_kind]["auth_uri"] = "https://accounts.google.com/o/oauth2/v2/auth"
        flow = InstalledAppFlow.from_client_config(client_config, SCOPES, state=state)
        flow.redirect_uri = redirect_uri
        return flow

    def _write_token(self, creds: Credentials, *, require_refresh: bool = False) -> None:
        with self._token_lock():
            self._write_token_unlocked(creds, require_refresh=require_refresh)

    def _write_token_unlocked(self, creds: Credentials, *, require_refresh: bool = False) -> None:
        """Atomically persist credentials, retaining an existing refresh grant."""
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        payload = json.loads(creds.to_json())
        old_payload: dict = {}
        old_stat = None
        if self.token_file.exists():
            old_stat = self.token_file.stat()
            try:
                old_payload = json.loads(self.token_file.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                old_payload = {}
        if not payload.get("refresh_token") and old_payload.get("refresh_token"):
            payload["refresh_token"] = old_payload["refresh_token"]
        if require_refresh and not payload.get("refresh_token"):
            raise RuntimeError("OAuth authorization did not return an offline refresh grant")

        fd, tmp_name = tempfile.mkstemp(prefix=f".{self.token_file.name}.", dir=self.token_file.parent)
        tmp_path = Path(tmp_name)
        try:
            harden_fd(fd)
            if old_stat is not None:
                keep_owner(fd, old_stat)
            data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            with os.fdopen(fd, "wb", closefd=True) as handle:
                fd = -1
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self.token_file)
            harden_path(self.token_file)
            try:
                directory_fd = os.open(self.token_file.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass  # directory fsync is not supported by every filesystem
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass

    def drive(self):
        return build("drive", "v3", credentials=self.credentials(), cache_discovery=False)

    def calendar(self):
        return build("calendar", "v3", credentials=self.credentials(), cache_discovery=False)

    def metadata(self, file_id: str) -> DriveMeta:
        raw = self.drive().files().get(fileId=file_id, fields="id,name,version,md5Checksum,modifiedTime,size").execute(num_retries=RETRIES)
        return DriveMeta(raw["id"], raw.get("name", ""), str(raw.get("version", "")), raw.get("md5Checksum", ""), raw.get("modifiedTime", ""), str(raw.get("size", "")))

    def download(self, file_id: str, destination: Path) -> str:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("wb") as handle:
            downloader = MediaIoBaseDownload(handle, self.drive().files().get_media(fileId=file_id))
            done = False
            while not done:
                _, done = downloader.next_chunk()
        digest = hashlib.md5()  # nosec: Drive integrity field is MD5
        with destination.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def list_owned_events(self, calendar_id: str, file_id: str) -> list[dict]:
        service = self.calendar(); token = None; output = []
        while True:
            result = service.events().list(calendarId=calendar_id, privateExtendedProperty=f"source_file_id={file_id}", singleEvents=True, maxResults=2500, pageToken=token, showDeleted=False).execute(num_retries=RETRIES)
            output.extend(result.get("items", [])); token = result.get("nextPageToken")
            if not token: return output

    def create_event(self, calendar_id: str, body: dict) -> dict:
        return self.calendar().events().insert(calendarId=calendar_id, body=body, sendUpdates="none").execute(num_retries=RETRIES)

    def update_event(self, calendar_id: str, event_id: str, body: dict) -> dict:
        return self.calendar().events().update(calendarId=calendar_id, eventId=event_id, body=body, sendUpdates="none").execute(num_retries=RETRIES)

    def delete_event(self, calendar_id: str, event_id: str) -> None:
        self.calendar().events().delete(calendarId=calendar_id, eventId=event_id, sendUpdates="none").execute(num_retries=RETRIES)

    def has_token(self) -> bool:
        return self.token_file.exists()

    def check_access(self, file_id: str) -> str:
        """Verify Drive + Calendar reachability and return the source file name."""
        meta = self.metadata(file_id)
        self.calendar().calendarList().list(maxResults=1).execute(num_retries=RETRIES)
        return meta.name

    def onboard(self, name: str, email: str, existing_calendar_id: str | None = None) -> OnboardResult:
        return self.onboard_with_prefix(name, email, "Calendar Sync – ", existing_calendar_id)

    def onboard_with_prefix(self, name: str, email: str, prefix: str, existing_calendar_id: str | None = None,
                            timezone_name: str = "Europe/Prague", summary: str | None = None) -> OnboardResult:
        service = self.calendar(); calendar_id = existing_calendar_id
        if not calendar_id:
            calendar_id = service.calendars().insert(
                body={"summary": summary or f"{prefix}{name}", "timeZone": timezone_name}).execute(num_retries=RETRIES)["id"]
        rules = service.acl().list(calendarId=calendar_id).execute(num_retries=RETRIES).get("items", [])
        exists = any(x.get("role") == "reader" and (x.get("scope") or {}).get("type") == "user" and (x.get("scope") or {}).get("value", "").casefold() == email.casefold() for x in rules)
        sent = False
        if not exists:
            service.acl().insert(calendarId=calendar_id, sendNotifications=True, body={"role": "reader", "scope": {"type": "user", "value": email}}).execute(num_retries=RETRIES); sent = True
        link = "https://calendar.google.com/calendar/u/0/r?cid=" + quote(calendar_id, safe="")
        return OnboardResult(calendar_id, link, sent, exists)


MAX_CLIENT_JSON_BYTES = 20_000


class ClientConfigError(ValueError):
    """The supplied OAuth client JSON is unusable. The message never contains secret values."""


def validate_client_config(text: str) -> dict:
    """Parse and validate a Google OAuth *Desktop app* client JSON; returns the parsed document."""
    if len(text.encode("utf-8")) > MAX_CLIENT_JSON_BYTES:
        raise ClientConfigError("Soubor je příliš velký na OAuth klienta.")
    try:
        document = json.loads(text)
    except ValueError:
        raise ClientConfigError("Obsah není platný JSON.") from None
    if not isinstance(document, dict):
        raise ClientConfigError("JSON musí být objekt stažený z Google Cloud.")
    if "installed" not in document:
        if "web" in document:
            raise ClientConfigError("Toto je klient typu „Web application“. Vytvořte klienta typu „Desktop app“.")
        raise ClientConfigError("JSON neobsahuje klíč „installed“ – očekává se klient typu „Desktop app“.")
    installed = document["installed"]
    if not isinstance(installed, dict):
        raise ClientConfigError("Klíč „installed“ má neplatný tvar.")
    for key in ("client_id", "client_secret", "token_uri"):
        if not isinstance(installed.get(key), str) or not installed[key].strip():
            raise ClientConfigError(f"V JSON chybí povinná položka „{key}“.")
    if not installed["client_id"].endswith(".apps.googleusercontent.com"):
        raise ClientConfigError("client_id nevypadá jako Google OAuth klient (.apps.googleusercontent.com).")
    if not installed["token_uri"].startswith("https://"):
        raise ClientConfigError("token_uri musí být https adresa.")
    return document


def client_id_hint(document: dict) -> str:
    """Non-secret, shortened client id for display."""
    client_id = str((document.get("installed") or {}).get("client_id", ""))
    return client_id[:12] + "…" + client_id[-28:] if len(client_id) > 44 else client_id


def install_client_file(text: str, destination: Path) -> dict:
    """Validate and atomically write the client JSON with owner-only permissions."""
    document = validate_client_config(text)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    tmp_path = Path(tmp_name)
    try:
        harden_fd(fd)
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, destination)
        harden_path(destination)
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
    return document
