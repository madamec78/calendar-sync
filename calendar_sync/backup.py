from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

from .setup import RuntimeConfig


def backup_directory(data_dir: Path, relative: str) -> Path:
    base = (data_dir / "backups").resolve()
    suffix = "" if relative == "backups" else relative.removeprefix("backups/")
    target = (base / suffix).resolve()
    if target != base and base not in target.parents:
        raise ValueError("Cíl záloh musí být pod /data/backups")
    target.mkdir(parents=True, exist_ok=True)
    return target


def _database_path(database_url: str) -> Path:
    if not database_url.startswith("sqlite:///"):
        raise ValueError("Záloha podporuje pouze SQLite")
    return Path(database_url.split("///", 1)[1]).resolve()


def create_backup(
    database_url: str,
    data_dir: Path,
    config: RuntimeConfig,
    now: dt.datetime | None = None,
) -> Path:
    source = _database_path(database_url)
    target_dir = backup_directory(data_dir, config.backup_directory)
    now = now or dt.datetime.now(dt.UTC)
    target = target_dir / f"calendar-sync-{now:%Y%m%dT%H%M%SZ}.sqlite"
    with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
        src.backup(dst)
    target.chmod(0o600)
    prune_backups(target_dir, config.backup_retention_count, config.backup_retention_days, now)
    return target


def prune_backups(
    directory: Path,
    keep_count: int,
    keep_days: int,
    now: dt.datetime | None = None,
) -> list[Path]:
    now = now or dt.datetime.now(dt.UTC)
    files = sorted(directory.glob("calendar-sync-*.sqlite"), key=lambda p: p.stat().st_mtime, reverse=True)
    removed: list[Path] = []
    cutoff = now.timestamp() - keep_days * 86400
    for index, path in enumerate(files):
        if index >= keep_count or path.stat().st_mtime < cutoff:
            path.unlink()
            removed.append(path)
    return removed
