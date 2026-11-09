from __future__ import annotations

from pathlib import Path

import pytest

from calendar_sync.config import Settings


class FakeGoogle:
    def __init__(self): self.onboard_calls = []; self.events = []; self.oauth_calls = []
    def onboard(self, name, email, existing_calendar_id=None):
        self.onboard_calls.append((name, email, existing_calendar_id))
        return existing_calendar_id or "cal-123@group.calendar.google.com", "https://calendar.google.com/calendar/u/0/r?cid=cal-123"
    def onboard_with_prefix(self, name, email, prefix, existing_calendar_id=None, timezone_name="Europe/Prague", summary=None):
        return self.onboard(name, email, existing_calendar_id)
    def authorization_url(self, redirect_uri, state):
        self.oauth_calls.append(("authorize", redirect_uri, state))
        return f"https://accounts.google.test/auth?state={state}"
    def complete_authorization(self, redirect_uri, state, code):
        self.oauth_calls.append(("complete", redirect_uri, state, code))
    def credentials(self):
        return object()


@pytest.fixture
def settings(tmp_path: Path):
    return Settings(tmp_path, f"sqlite:///{tmp_path / 'test.db'}", "file-1", "correct horse battery staple",
                    "x" * 48, "https://testserver", False, 120, 60, False,
                    tmp_path / "client.json", tmp_path / "token.json", "INFO")
