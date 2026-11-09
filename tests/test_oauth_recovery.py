from __future__ import annotations

import datetime as dt
import json
import os
import stat
import threading
import time

import pytest
from conftest import FakeGoogle
from fastapi.testclient import TestClient
from google.auth.exceptions import RefreshError
from test_web import login

from calendar_sync.google import GoogleClient, OAuthReauthorizationRequired, OAuthTransientError
from calendar_sync.models import SyncRun
from calendar_sync.setup import RuntimeConfig, get_setting, save_runtime, set_setting
from calendar_sync.web import create_app


class ExpiredGoogle(FakeGoogle):
    def credentials(self):
        raise OAuthReauthorizationRequired("sensitive provider response")


class RefreshRejectedCredentials:
    expired = True
    refresh_token = "present"
    valid = False

    def refresh(self, _request):
        raise RefreshError("invalid_grant: provider detail must not escape")


def activate_due_worker(app) -> None:
    with app.state.sessions() as db:
        save_runtime(db, RuntimeConfig(drive_file_id="drive-1", interval_minutes=15))
        set_setting(db, "setup_complete", True)
        set_setting(db, "next_sync_at", (dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)).isoformat())
        db.commit()


def test_invalid_grant_is_classified_without_exposing_provider_detail(settings, monkeypatch):
    settings.oauth_token_file.write_text("{}", encoding="utf-8")
    client = GoogleClient(settings.oauth_client_file, settings.oauth_token_file)
    monkeypatch.setattr(
        "calendar_sync.google.Credentials.from_authorized_user_file",
        lambda *_args, **_kwargs: RefreshRejectedCredentials(),
    )

    with pytest.raises(OAuthReauthorizationRequired, match="must be renewed") as caught:
        client.credentials()

    assert "invalid_grant" not in str(caught.value)


def test_readiness_rejects_revoked_oauth_and_sanitizes_response(settings):
    settings.oauth_token_file.write_text("{}", encoding="utf-8")
    app = create_app(settings, ExpiredGoogle())
    activate_due_worker(app)

    with TestClient(app) as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    assert "Google OAuth reauthorization required" in response.text
    assert "sensitive provider response" not in response.text


def test_scheduler_records_failure_advances_due_time_and_ui_offers_recovery(settings, monkeypatch):
    settings.oauth_token_file.write_text("{}", encoding="utf-8")
    app = create_app(settings, ExpiredGoogle())
    activate_due_worker(app)
    monkeypatch.setattr(
        app.state.service,
        "poll",
        lambda: (_ for _ in ()).throw(OAuthReauthorizationRequired("private detail")),
    )

    before = dt.datetime.now(dt.UTC)
    app.state.scheduled_tick()

    with app.state.sessions() as db:
        next_due = dt.datetime.fromisoformat(get_setting(db, "next_sync_at"))
        failure = get_setting(db, "worker_last_error")
        run = db.query(SyncRun).order_by(SyncRun.id.desc()).first()
        assert next_due >= before + dt.timedelta(minutes=14)
        assert failure["code"] == "oauth_reauthorization_required"
        assert "private detail" not in failure["message"]
        assert run.status == "error" and run.error == "oauth_reauthorization_required"

    with TestClient(app) as client:
        login(client)
        page = client.get("/")
        assert "Poslední běh workeru selhal" in page.text
        assert "Obnovit Google OAuth" in page.text
        assert "private detail" not in page.text


def test_successful_oauth_callback_clears_worker_failure(settings):
    settings.oauth_token_file.write_text("{}", encoding="utf-8")
    fake = FakeGoogle()
    app = create_app(settings, fake)
    with app.state.sessions() as db:
        set_setting(db, "worker_last_error", {"code": "oauth_reauthorization_required", "message": "expired"})
        db.commit()

    with TestClient(app) as client:
        login(client)
        authorize = client.get("/oauth/authorize", follow_redirects=False)
        state = authorize.headers["location"].split("state=", 1)[1]
        callback = client.get(f"/oauth/callback?state={state}&code=new-code")
        assert callback.status_code == 200

    with app.state.sessions() as db:
        assert get_setting(db, "worker_last_error") is None


class SerializableCredentials:
    def __init__(self, access="access-new", refresh="refresh-new", *, expired=False, valid=True):
        self.token = access
        self.refresh_token = refresh
        self.expired = expired
        self.valid = valid

    def to_json(self):
        payload = {"token": self.token, "client_id": "id", "client_secret": "secret"}
        if self.refresh_token is not None:
            payload["refresh_token"] = self.refresh_token
        return json.dumps(payload)

    def refresh(self, _request):
        self.token = "access-refreshed"
        self.refresh_token = "refresh-rotated"
        self.expired = False
        self.valid = True


class FakeFlow:
    def __init__(self, redirect_uri, credentials=None, error=None):
        self.redirect_uri = redirect_uri
        self.credentials = credentials or SerializableCredentials()
        self.error = error
        self.code = None

    def fetch_token(self, *, code):
        self.code = code
        if self.error:
            raise self.error


def test_authorization_url_requests_offline_consent_and_keeps_state(settings, monkeypatch):
    client = GoogleClient(settings.oauth_client_file, settings.oauth_token_file)
    calls = {}

    class Flow:
        redirect_uri = "http://localhost:8081/"

        def authorization_url(self, **kwargs):
            calls.update(kwargs)
            return "https://example.test/auth", "state"

    flow = Flow()
    monkeypatch.setattr(client, "_web_flow", lambda redirect, state: flow)
    assert client.authorization_url(flow.redirect_uri, "csrf-state") == "https://example.test/auth"
    assert calls["access_type"] == "offline"
    assert calls["prompt"] == "consent"
    assert client._pending_flows["csrf-state"] is flow


def test_code_exchange_requires_and_persists_refresh_token(settings):
    client = GoogleClient(settings.oauth_client_file, settings.oauth_token_file)
    flow = FakeFlow("http://localhost:8081/", SerializableCredentials(refresh="grant"))
    client._pending_flows["state"] = flow
    client.complete_authorization(flow.redirect_uri, "state", "one-time-code")
    stored = json.loads(settings.oauth_token_file.read_text())
    assert flow.code == "one-time-code"
    assert stored["refresh_token"] == "grant"
    assert os.name == "nt" or stat.S_IMODE(settings.oauth_token_file.stat().st_mode) == 0o600


def test_code_exchange_sanitizes_provider_error(settings):
    client = GoogleClient(settings.oauth_client_file, settings.oauth_token_file)
    raw = "client_secret=secret token=access raw-provider-body"
    flow = FakeFlow("http://localhost:8081/", error=RuntimeError(raw))
    client._pending_flows["state"] = flow
    with pytest.raises(OAuthTransientError, match="code exchange failed") as caught:
        client.complete_authorization(flow.redirect_uri, "state", "secret-code")
    assert raw not in str(caught.value)
    assert caught.value.__cause__ is None
    assert not settings.oauth_token_file.exists()


def test_atomic_write_preserves_old_refresh_token_and_owner(settings):
    settings.oauth_token_file.write_text(json.dumps({"refresh_token": "old-grant", "token": "old"}))
    before = settings.oauth_token_file.stat()
    client = GoogleClient(settings.oauth_client_file, settings.oauth_token_file)
    client._write_token(SerializableCredentials(refresh=None))
    stored = json.loads(settings.oauth_token_file.read_text())
    after = settings.oauth_token_file.stat()
    assert stored["refresh_token"] == "old-grant"
    assert (after.st_uid, after.st_gid) == (before.st_uid, before.st_gid)
    assert os.name == "nt" or stat.S_IMODE(after.st_mode) == 0o600
    leftovers = [
        path
        for path in settings.oauth_token_file.parent.glob(f".{settings.oauth_token_file.name}.*")
        if path.name != f".{settings.oauth_token_file.name}.lock"
    ]
    assert not leftovers


def test_cached_expired_credentials_persist_access_and_refresh_rotation(settings, monkeypatch):
    settings.oauth_token_file.write_text(json.dumps({"refresh_token": "old-grant", "token": "old"}))
    creds = SerializableCredentials(access="old", refresh="old-grant", expired=True, valid=False)
    monkeypatch.setattr(
        "calendar_sync.google.Credentials.from_authorized_user_file", lambda *_args, **_kwargs: creds
    )
    client = GoogleClient(settings.oauth_client_file, settings.oauth_token_file)
    result = client.credentials()
    stored = json.loads(settings.oauth_token_file.read_text())
    assert result.valid
    assert stored["token"] == "access-refreshed"
    assert stored["refresh_token"] == "refresh-rotated"


def test_concurrent_writers_are_serialized_and_leave_complete_token(settings, monkeypatch):
    clients = [GoogleClient(settings.oauth_client_file, settings.oauth_token_file) for _ in range(4)]
    active = 0
    peak = 0
    guard = threading.Lock()
    original = GoogleClient._write_token_unlocked

    def observed(self, creds, *, require_refresh=False):
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        time.sleep(0.02)
        try:
            return original(self, creds, require_refresh=require_refresh)
        finally:
            with guard:
                active -= 1

    monkeypatch.setattr(GoogleClient, "_write_token_unlocked", observed)
    threads = [
        threading.Thread(target=c._write_token, args=(SerializableCredentials(access=f"a{i}", refresh=f"r{i}"),))
        for i, c in enumerate(clients)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    stored = json.loads(settings.oauth_token_file.read_text())
    assert peak == 1
    assert stored["token"].startswith("a") and stored["refresh_token"].startswith("r")


def test_transient_refresh_error_is_sanitized(settings, monkeypatch):
    settings.oauth_token_file.write_text("{}")

    class Failed(RefreshRejectedCredentials):
        def refresh(self, _request):
            raise RefreshError("token=secret client_secret=secret raw-provider-body")

    monkeypatch.setattr(
        "calendar_sync.google.Credentials.from_authorized_user_file", lambda *_args, **_kwargs: Failed()
    )
    with pytest.raises(OAuthTransientError, match="temporarily failed") as caught:
        GoogleClient(settings.oauth_client_file, settings.oauth_token_file).credentials()
    assert "secret" not in str(caught.value)
    assert caught.value.__cause__ is None
