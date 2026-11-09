from __future__ import annotations

import re

import pytest
from conftest import FakeGoogle
from fastapi.testclient import TestClient

from calendar_sync.google import OAuthReauthorizationRequired
from calendar_sync.models import Technician
from calendar_sync.setup import get_setting, set_setting
from calendar_sync.web import create_app


def token(response):
    return re.search(r"name=['\"]csrf_token['\"] value=['\"]([^'\"]+)['\"]", response.text).group(1)


def login(client):
    response = client.get("/login")
    return client.post("/login", data={"password": "correct horse battery staple", "csrf_token": token(response)}, follow_redirects=False)


def activate_dashboard(app):
    with app.state.sessions() as db:
        set_setting(db, "setup_complete", True)
        db.commit()


def test_auth_and_csrf(settings):
    app = create_app(settings, FakeGoogle())
    with TestClient(app) as client:
        unauthenticated = client.get("/")
        assert unauthenticated.status_code == 200 and unauthenticated.url.path == "/login"
        assert login(client).status_code == 303
        assert client.get("/").status_code == 200
        assert client.post("/technicians", data={"workbook_name": "X", "email": "x@example.test", "csrf_token": "bad"}).status_code == 403


def test_onboarding_updates_calendar_and_link(settings):
    fake = FakeGoogle(); app = create_app(settings, fake)
    with TestClient(app) as client:
        login(client); home = client.get("/"); csrf = token(home)
        response = client.post("/technicians", data={"workbook_name": "Test Technik", "email": "a@example.test", "enabled": "on", "csrf_token": csrf}, follow_redirects=False)
        assert response.status_code == 303
        with app.state.sessions() as db: tech = db.query(Technician).one(); tech_id = tech.id
        csrf = token(client.get("/"))
        response = client.post(f"/technicians/{tech_id}/onboard", data={"csrf_token": csrf}, follow_redirects=False)
        assert response.status_code == 200
        assert "Jednorázová pozvánka ke sdílenému kalendáři byla odeslána" in response.text
        assert "Nejde o pozvánku k události" in response.text
        assert fake.onboard_calls == [("Test Technik", "a@example.test", None)]
        with app.state.sessions() as db:
            tech = db.get(Technician, tech_id)
            assert tech.calendar_id.startswith("cal-123") and tech.onboarding_status == "shared" and "calendar.google.com" in tech.onboarding_link
            assert get_setting(db, "force_sync") is True
            assert get_setting(db, "next_sync_at")


def test_existing_calendar_id_can_be_saved_before_onboarding(settings):
    app = create_app(settings, FakeGoogle())
    with TestClient(app) as client:
        login(client)
        response = client.post(
            "/technicians",
            data={"workbook_name": "Trmal Matěj", "email": "t@example.test", "enabled": "on", "csrf_token": token(client.get("/"))},
            follow_redirects=False,
        )
        assert response.status_code == 303
        with app.state.sessions() as db:
            tech_id = db.query(Technician).one().id
        response = client.post(
            f"/technicians/{tech_id}/update",
            data={"email": "t@example.test", "calendar_id": "existing@group.calendar.google.com", "enabled": "on", "csrf_token": token(client.get("/"))},
            follow_redirects=False,
        )
        assert response.status_code == 303
        with app.state.sessions() as db:
            assert db.get(Technician, tech_id).calendar_id == "existing@group.calendar.google.com"


def test_file_import_routes_are_removed(settings):
    app = create_app(settings, FakeGoogle())
    with TestClient(app) as client:
        login(client)
        assert client.get("/technicians/import").status_code == 404
        assert client.get("/technicians/template.csv").status_code == 404
        assert client.post("/setup/workbook-mapping/fixture").status_code == 404
        page = client.get("/setup")
        assert "CSV import" not in page.text
        assert "Přiřadit e-maily technikům" in page.text


def test_web_oauth_requires_auth_and_round_trips_state_without_exposing_code(settings):
    fake = FakeGoogle(); app = create_app(settings, fake)
    with TestClient(app) as client:
        assert client.get("/oauth/authorize", follow_redirects=False).status_code == 303
        login(client)
        response = client.get("/oauth/authorize", follow_redirects=False)
        assert response.status_code == 302
        assert response.headers["location"].startswith("https://accounts.google.test/auth?state=")
        state = response.headers["location"].split("state=", 1)[1]
        callback = client.get(f"/oauth/callback?state={state}&code=one-time-code")
        assert callback.status_code == 200
        assert "bezpečně uložen" in callback.text
        assert "one-time-code" not in callback.text
        assert fake.oauth_calls[-1] == ("complete", "http://localhost:8081/", state, "one-time-code")


def test_web_oauth_rejects_state_reuse_and_csrf_is_not_bypassed(settings):
    fake = FakeGoogle(); app = create_app(settings, fake)
    with TestClient(app) as client:
        login(client)
        response = client.get("/oauth/authorize", follow_redirects=False)
        state = response.headers["location"].split("state=", 1)[1]
        assert client.get("/oauth/callback?state=wrong&code=secret").status_code == 403
        response = client.get("/oauth/authorize", follow_redirects=False)
        state = response.headers["location"].split("state=", 1)[1]
        assert client.get(f"/oauth/callback?state={state}&code=secret").status_code == 200
        assert client.get(f"/oauth/callback?state={state}&code=secret").status_code == 403


def test_manual_local_oauth_callback_can_be_pasted(settings):
    fake = FakeGoogle(); app = create_app(settings, fake)
    with TestClient(app) as client:
        login(client)
        response = client.get("/oauth/authorize", follow_redirects=False)
        state = response.headers["location"].split("state=", 1)[1]
        form = client.get("/oauth/manual")
        callback = f"http://localhost:8081/?state={state}&code=one-time-code"
        result = client.post("/oauth/manual", data={"callback_url": callback, "csrf_token": token(form)})
        assert result.status_code == 200
        assert "bezpečně uložen" in result.text
        assert fake.oauth_calls[-1] == ("complete", "http://localhost:8081/", state, "one-time-code")


def test_every_admin_page_renders_with_primary_navigation(settings):
    settings.oauth_token_file.write_text("{}", encoding="utf-8")
    app = create_app(settings, FakeGoogle())
    activate_dashboard(app)
    with TestClient(app) as client:
        login(client)
        for path in ("/", "/technicians", "/runs", "/stale", "/audit", "/settings", "/setup", "/setup/workbook-mapping"):
            page = client.get(path)
            assert page.status_code == 200, path
            nav = re.search(r"<nav class=\"nav\".*?</nav>", page.text, re.DOTALL).group(0)
            for label in ("Přehled", "Technici", "Historie", "Audit", "Nastavení", "Průvodce"):
                assert label in nav
        dashboard = client.get("/")
        assert 'action="/sync"' in dashboard.text and "Synchronizovat nyní" in dashboard.text
        settings_page = client.get("/settings")
        assert 'action="/settings/save"' in settings_page.text and 'name="drive_file"' in settings_page.text
        assert "Změna zdroje nebo pravidel" in settings_page.text
        assert client.get("/runs/999").status_code == 404
        guide = client.get("/help")
        assert guide.status_code == 200
        for needle in ("console.cloud.google.com/auth/audience", "In production", "Desktop app", "localhost:8081", "drive.readonly", "Co očekává od správce", "dvou po sobě jdoucích", "Zapomenuté heslo správce", "calendar-sync set-password"):
            assert needle in guide.text
        assert client.get("/help/google", follow_redirects=False).headers["location"] == "/help#google"


def test_security_headers_and_no_inline_scripts(settings):
    app = create_app(settings, FakeGoogle())
    with TestClient(app) as client:
        response = client.get("/login")
        assert "default-src 'self'" in response.headers["content-security-policy"]
        assert response.headers["x-frame-options"] == "DENY"
        assert "<script>" not in response.text and "style=" not in response.text
        assert client.get("/static/app.css").status_code == 200


def test_login_is_rate_limited(settings):
    app = create_app(settings, FakeGoogle())
    with TestClient(app) as client:
        codes = []
        for _ in range(8):
            page = client.get("/login")
            codes.append(client.post("/login", data={"password": "wrong", "csrf_token": token(page)}).status_code)
        assert codes[:6] == [401] * 6 and codes[-1] == 429


def test_local_public_url_uses_direct_oauth_callback(tmp_path):
    from calendar_sync.config import Settings
    local = Settings(tmp_path, f"sqlite:///{tmp_path / 'l.db'}", "f", "correct horse battery staple", "x" * 48,
                     "http://localhost:8080", False, 120, 60, False, tmp_path / "c.json", tmp_path / "t.json", "INFO")
    fake = FakeGoogle(); app = create_app(local, fake)
    with TestClient(app) as client:
        login(client)
        response = client.get("/oauth/authorize", follow_redirects=False)
        state = response.headers["location"].split("state=", 1)[1]
        assert fake.oauth_calls[0][1] == "http://localhost:8080/oauth/callback"
        assert client.get(f"/oauth/callback?state={state}&code=c").status_code == 200
        assert fake.oauth_calls[-1][1] == "http://localhost:8080/oauth/callback"


def test_internal_workbook_mapping_route_remains_available(settings):
    app = create_app(settings, FakeGoogle())
    with TestClient(app) as client:
        login(client)
        page = client.get("/setup/workbook-mapping")

    assert page.status_code == 200
    assert 'action="/setup/workbook-mapping/test"' in page.text


def test_status_shows_live_ready_and_usable_oauth(settings):
    settings.oauth_token_file.write_text("{}", encoding="utf-8")
    app = create_app(settings, FakeGoogle())
    activate_dashboard(app)
    with TestClient(app) as client:
        login(client)
        page = client.get("/")
    assert "připraven" in page.text and "nepřipraven" not in page.text
    assert "Google OAuth: použitelná" in page.text
    assert "Obnovit Google OAuth" not in page.text


def test_status_shows_missing_oauth_and_reauthorization_cta(settings):
    app = create_app(settings, FakeGoogle())
    activate_dashboard(app)
    with TestClient(app) as client:
        login(client)
        page = client.get("/")
    assert "Google OAuth: chybí" in page.text and "Autorizovat Google" in page.text
    assert "nepřipraven" in page.text


def test_status_shows_broken_oauth_and_worker_failure(settings):
    class BrokenGoogle(FakeGoogle):
        def credentials(self):
            raise OAuthReauthorizationRequired("provider detail")

    settings.oauth_token_file.write_text("{}", encoding="utf-8")
    app = create_app(settings, BrokenGoogle())
    activate_dashboard(app)
    with app.state.sessions() as db:
        set_setting(db, "worker_last_error", {"code": "worker_error", "message": "Synchronizace selhala."})
        db.commit()
    with TestClient(app) as client:
        login(client)
        page = client.get("/")
    assert "nepřipraven" in page.text and "Synchronizace selhala." in page.text
    assert "provider detail" not in page.text


VALID_CLIENT = ('{"installed":{"client_id":"123456789012-abcdefghijklmnopqrstuvwxyz012345.apps.googleusercontent.com",'
                '"client_secret":"TOPSECRET-value","token_uri":"https://oauth2.googleapis.com/token",'
                '"auth_uri":"https://accounts.google.com/o/oauth2/auth"}}')


def test_oauth_client_json_can_be_pasted_validated_and_never_echoed(settings):
    import json as _json
    app = create_app(settings, FakeGoogle())
    with TestClient(app) as client:
        assert client.post("/settings/oauth-client", data={"client_json": VALID_CLIENT, "csrf_token": "x"}).status_code == 401
        login(client)
        csrf = token(client.get("/settings"))
        assert client.post("/settings/oauth-client", data={"client_json": VALID_CLIENT, "csrf_token": "bad"}).status_code == 403
        for bad, expected in (("{nope", "platný JSON"), ('{"web":{}}', "Desktop app"), ('{"installed":{"client_id":"x"}}', "client_secret")):
            page = client.post("/settings/oauth-client", data={"client_json": bad, "csrf_token": csrf})
            assert expected in page.text and "TOPSECRET" not in page.text
        assert not settings.oauth_client_file.exists()
        ok = client.post("/settings/oauth-client", data={"client_json": VALID_CLIENT, "csrf_token": csrf})
        assert "OAuth klient byl uložen" in ok.text and "TOPSECRET" not in ok.text
        assert _json.loads(settings.oauth_client_file.read_text())["installed"]["client_secret"] == "TOPSECRET-value"
        assert "klient nahrán" in client.get("/settings").text and "TOPSECRET" not in client.get("/help").text
        with app.state.sessions() as db:
            from calendar_sync.models import AuditLog
            assert "TOPSECRET" not in "".join(x.details_json for x in db.query(AuditLog))


def test_oauth_client_file_upload_and_readonly_mount_falls_back_to_data_volume(settings, monkeypatch):
    from calendar_sync import web
    real = web.install_client_file
    calls = []

    def flaky(text, destination):
        calls.append(destination)
        if destination == settings.oauth_client_file:
            raise PermissionError("read-only")
        return real(text, destination)

    monkeypatch.setattr(web, "install_client_file", flaky)
    app = create_app(settings, FakeGoogle())
    with TestClient(app) as client:
        login(client)
        csrf = token(client.get("/settings"))
        client.post("/settings/oauth-client", data={"csrf_token": csrf},
                    files={"client_file": ("client.json", VALID_CLIENT.encode(), "application/json")})
    assert calls[-1] == settings.data_dir / "google_oauth_client.json" and calls[-1].exists()



def test_admin_password_can_be_changed_in_ui_and_is_stored_hashed(settings):
    from calendar_sync.auth import HASH_SETTING_KEY
    from calendar_sync.models import AuditLog
    app = create_app(settings, FakeGoogle())
    old, new = "correct horse battery staple", "my-own-new-passphrase-1"
    with TestClient(app) as client:
        login(client)
        csrf = token(client.get("/settings"))

        def change(**fields):
            return client.post("/settings/password", data={"csrf_token": csrf, **fields}, follow_redirects=False)

        assert "Současné heslo není správné" in client.get(change(current_password="bad", new_password=new, confirm_password=new).headers["location"]).text
        csrf = token(client.get("/settings"))
        for fields, expected in (({"new_password": "short", "confirm_password": "short"}, "alespoň 12"),
                                 ({"new_password": new, "confirm_password": new + "x"}, "neshodují"),
                                 ({"new_password": old, "confirm_password": old}, "jiné než současné")):
            response = change(current_password=old, **fields)
            assert expected in client.get(response.headers["location"]).text
            csrf = token(client.get("/settings"))
        ok = change(current_password=old, new_password=new, confirm_password=new)
        assert ok.status_code == 303 and ok.headers["location"] == "/login"
        assert client.get("/settings", follow_redirects=False).status_code == 303  # session was cleared
        with app.state.sessions() as db:
            from calendar_sync.setup import get_setting
            stored = get_setting(db, HASH_SETTING_KEY)
            assert stored.startswith("scrypt$") and new not in stored
            assert new not in "".join(x.details_json for x in db.query(AuditLog))
            assert any(x.action == "admin_password_changed" for x in db.query(AuditLog))
        page = client.get("/login")
        assert client.post("/login", data={"password": old, "csrf_token": token(page)}).status_code == 401
        page = client.get("/login")
        assert client.post("/login", data={"password": new, "csrf_token": token(page)}, follow_redirects=False).status_code == 303


def test_cli_set_password_reads_stdin_and_enforces_policy(settings, monkeypatch, capsys):
    import io
    import sys

    from calendar_sync import cli
    from calendar_sync.models import init_db, make_session_factory
    from calendar_sync.setup import verify_admin_password
    engine, sessions = make_session_factory(settings.database_url)
    init_db(engine)
    monkeypatch.setattr(cli.Settings, "from_env", classmethod(lambda cls: settings))
    monkeypatch.setattr(sys, "argv", ["calendar-sync", "set-password"])
    monkeypatch.setattr(sys, "stdin", io.StringIO("short\n"))
    with pytest.raises(SystemExit) as refused:
        cli.main()
    assert refused.value.code == 2
    monkeypatch.setattr(sys, "stdin", io.StringIO("a-long-enough-passphrase\n"))
    cli.main()
    with sessions() as db:
        assert verify_admin_password(db, "a-long-enough-passphrase", settings.admin_password)
        assert not verify_admin_password(db, settings.admin_password, settings.admin_password)
