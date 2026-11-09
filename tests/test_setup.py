import datetime as dt

from conftest import FakeGoogle
from fastapi.testclient import TestClient
from test_web import login

from calendar_sync.setup import RuntimeConfig, next_sync
from calendar_sync.web import create_app


def test_worker_is_hard_gated_before_setup(settings):
    fake = FakeGoogle(); fake.metadata = lambda *_: (_ for _ in ()).throw(AssertionError("worker touched Google"))
    app = create_app(settings, fake)
    app.state.scheduled_tick()  # must return before any external client call


def test_first_run_gate_redirects_to_setup(settings):
    app = create_app(settings, FakeGoogle())
    with TestClient(app) as client:
        login(client)
        response = client.get("/", follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/setup"


def test_daily_schedule_and_dst_gap_europe_prague():
    cfg = RuntimeConfig(drive_file_id="x", timezone="Europe/Prague", schedule_mode="times", daily_times=["02:30"])
    # 02:30 does not exist on 2026-03-29; preview advances to first valid local minute (03:00 CEST).
    now = dt.datetime(2026, 3, 28, 23, 0, tzinfo=dt.UTC)
    result = next_sync(now, cfg)
    assert result.isoformat() == "2026-03-29T03:00:00+02:00"


def test_interval_schedule():
    cfg = RuntimeConfig(drive_file_id="x", schedule_mode="interval", interval_minutes=15)
    now = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC)
    assert next_sync(now, cfg).astimezone(dt.UTC) == now + dt.timedelta(minutes=15)
