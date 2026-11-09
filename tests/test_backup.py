import datetime as dt
import os

from conftest import FakeGoogle

from calendar_sync.backup import create_backup, prune_backups
from calendar_sync.models import init_db, make_session_factory
from calendar_sync.setup import RuntimeConfig, get_setting, save_runtime, set_setting
from calendar_sync.web import create_app


def test_backup_and_retention(tmp_path):
    db_path = tmp_path / "app.sqlite"; engine, _ = make_session_factory(f"sqlite:///{db_path}"); init_db(engine)
    cfg = RuntimeConfig(drive_file_id="x", backup_directory="backups", backup_retention_count=2, backup_retention_days=30)
    made = create_backup(f"sqlite:///{db_path}", tmp_path, cfg, dt.datetime(2026, 1, 10, tzinfo=dt.UTC))
    assert made.exists() and made.parent == tmp_path / "backups"
    for index in range(3):
        path = made.parent / f"calendar-sync-old{index}.sqlite"; path.write_bytes(b"x"); os.utime(path, (1 + index, 1 + index))
    removed = prune_backups(made.parent, 2, 30, dt.datetime(2026, 1, 10, tzinfo=dt.UTC))
    assert removed and len(list(made.parent.glob("*.sqlite"))) <= 2


def test_scheduled_backup_helper_creates_sqlite_backup_and_advances_due_time(settings):
    app = create_app(settings, FakeGoogle())
    runtime = RuntimeConfig(
        drive_file_id="file-1",
        backup_frequency="daily",
        backup_time="02:30",
        backup_directory="backups",
    )
    due = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)
    with app.state.sessions() as db:
        save_runtime(db, runtime)
        set_setting(db, "setup_complete", True)
        set_setting(db, "next_sync_at", None)
        set_setting(db, "next_backup_at", due.isoformat())
        db.commit()

    app.state.scheduled_tick()

    backups = list((settings.data_dir / "backups").glob("calendar-sync-*.sqlite"))
    assert len(backups) == 1
    with app.state.sessions() as db:
        next_due = dt.datetime.fromisoformat(str(get_setting(db, "next_backup_at")))
    assert next_due > due
