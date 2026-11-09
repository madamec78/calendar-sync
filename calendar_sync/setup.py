from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import asdict, dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import AppSetting, AuditLog

TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


@dataclass
class RuntimeConfig:
    deployment_name: str = "Calendar Sync"
    organization: str = "Calendar Sync"
    calendar_template: str = "{organization} – {technician}"
    timezone: str = "Europe/Prague"
    schedule_mode: str = "interval"
    interval_minutes: int = 2
    daily_times: list[str] = None  # type: ignore[assignment]
    stability_seconds: int = 60
    sync_horizon_days: int = 90
    sync_lookback_days: int = 0
    sheet_pattern: str = "*"
    technician_name_column: str = "A"
    header_scan_rows: int = 12
    first_data_column: str = ""
    auto_detect_dates: bool = True
    continuation_policy: str = "blank-name-continues"
    stale_policy: str = "two-snapshot"
    assignment_drop_threshold: float = 0.5
    color_map: dict[str, str] = None  # type: ignore[assignment]
    parse_phones: bool = True
    parse_addresses: bool = True
    backup_frequency: str = "daily"
    backup_time: str = "02:30"
    backup_retention_count: int = 14
    backup_retention_days: int = 30
    backup_directory: str = "backups"
    drive_file_id: str = ""
    calendar_prefix: str = "Calendar Sync – "  # v1 compatibility; calendar_template is authoritative
    delete_enabled: bool = False

    def __post_init__(self):
        if self.daily_times is None: self.daily_times = ["06:00"]
        if self.color_map is None: self.color_map = {"TMB": "3", "CTN": "9", "VDF": "11"}

    def validate(self) -> list[str]:
        errors = []
        try: ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError: errors.append("Neznámé časové pásmo")
        if self.schedule_mode not in {"interval", "times"}: errors.append("Neplatný režim plánování")
        if not 1 <= self.interval_minutes <= 10080: errors.append("Interval musí být 1–10080 minut")
        if not self.daily_times or any(not TIME_RE.match(x) for x in self.daily_times): errors.append("Časy musí být ve formátu HH:MM")
        if len(set(self.daily_times)) != len(self.daily_times): errors.append("Časy se nesmí opakovat")
        if not 0 <= self.stability_seconds <= 3600: errors.append("Stability delay musí být 0–3600 sekund")
        if self.backup_frequency not in {"disabled", "daily", "weekly"}: errors.append("Neplatná frekvence zálohy")
        if not TIME_RE.match(self.backup_time): errors.append("Čas zálohy musí být HH:MM")
        if not 1 <= self.backup_retention_count <= 1000: errors.append("Retence počtu musí být 1–1000")
        if not 1 <= self.backup_retention_days <= 3650: errors.append("Retence dnů musí být 1–3650")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", self.backup_directory) or ".." in self.backup_directory.split("/"): errors.append("Neplatný adresář záloh")
        if not self.drive_file_id.strip(): errors.append("Google Drive file ID je povinné")
        if not self.deployment_name.strip() or not self.organization.strip(): errors.append("Deployment a organization jsou povinné")
        if "{technician}" not in self.calendar_template: errors.append("Šablona kalendáře musí obsahovat {technician}")
        try: self.calendar_template.format(organization=self.organization, technician="Test")
        except (KeyError, ValueError): errors.append("Neplatná šablona kalendáře")
        if not re.fullmatch(r"[A-Za-z]+", self.technician_name_column): errors.append("Neplatný sloupec jména technika")
        if self.first_data_column and not re.fullmatch(r"[A-Za-z]+", self.first_data_column): errors.append("Neplatný první datový sloupec")
        if not 1 <= self.header_scan_rows <= 100: errors.append("Header scan musí být 1–100")
        if self.continuation_policy not in {"blank-name-continues", "strict"}: errors.append("Neplatná continuation policy")
        if self.stale_policy not in {"disabled", "two-snapshot"}: errors.append("Neplatná stale policy")
        if not 0 <= self.assignment_drop_threshold < 1: errors.append("Assignment threshold musí být 0–1")
        if not 1 <= self.sync_horizon_days <= 730: errors.append("Sync horizon musí být 1–730 dní")
        if not 0 <= self.sync_lookback_days <= 365: errors.append("Sync lookback musí být 0–365 dní")
        if any(not str(v).isdigit() or not 1 <= int(v) <= 11 for v in self.color_map.values()): errors.append("Google color ID musí být 1–11")
        if len(self.calendar_prefix) > 100: errors.append("Prefix kalendáře je příliš dlouhý")
        return errors


# Changing any of these invalidates the verified setup (parsing / identity / deletion semantics), so the
# worker gate closes until the wizard is run again. Everything else (schedule, backups, naming...) is applied live.
MATERIAL_FIELDS = (
    "drive_file_id", "timezone", "deployment_name", "sheet_pattern", "technician_name_column",
    "header_scan_rows", "first_data_column", "auto_detect_dates", "continuation_policy", "stale_policy",
    "assignment_drop_threshold", "color_map", "parse_phones", "parse_addresses", "sync_horizon_days",
    "sync_lookback_days", "delete_enabled",
)


def material_changes(before: RuntimeConfig, after: RuntimeConfig) -> list[str]:
    return [name for name in MATERIAL_FIELDS if getattr(before, name) != getattr(after, name)]


def calendar_name(config: RuntimeConfig, technician: str) -> str:
    """Display name of a technician's Google calendar (template is authoritative)."""
    try:
        name = config.calendar_template.format(organization=config.organization, technician=technician).strip()
    except (KeyError, IndexError, ValueError):
        name = ""
    return name or f"{config.calendar_prefix}{technician}"


def get_setting(db, key: str, default=None):
    row = db.get(AppSetting, key)
    return json.loads(row.value_json) if row else default


def set_setting(db, key: str, value, actor: str = "admin") -> None:
    before = get_setting(db, key)
    row = db.get(AppSetting, key)
    if row: row.value_json = json.dumps(value, ensure_ascii=False)
    else: db.add(AppSetting(key=key, value_json=json.dumps(value, ensure_ascii=False)))
    db.add(AuditLog(actor=actor, action="setting_changed", details_json=json.dumps({"key": key, "before": before, "after": value}, ensure_ascii=False)))


def load_runtime(db) -> RuntimeConfig:
    raw = get_setting(db, "runtime_config", {})
    return RuntimeConfig(**{k: v for k, v in raw.items() if k in RuntimeConfig.__dataclass_fields__})


def save_runtime(db, config: RuntimeConfig, actor: str = "admin") -> None:
    set_setting(db, "runtime_config", asdict(config), actor)


def setup_complete(db) -> bool:
    return bool(get_setting(db, "setup_complete", False))


def extract_drive_id(value: str) -> str:
    value = value.strip()
    match = re.search(r"/d/([A-Za-z0-9_-]+)", value) or re.search(r"[?&]id=([A-Za-z0-9_-]+)", value)
    return match.group(1) if match else value


def _valid_local(candidate: dt.datetime, zone: ZoneInfo) -> bool:
    aware = candidate.replace(tzinfo=zone)
    return aware.astimezone(dt.UTC).astimezone(zone).replace(tzinfo=None) == candidate


def next_sync(now: dt.datetime, config: RuntimeConfig) -> dt.datetime:
    zone = ZoneInfo(config.timezone); local_now = now.astimezone(zone)
    if config.schedule_mode == "interval": return local_now + dt.timedelta(minutes=config.interval_minutes)
    for day_offset in range(9):
        day = local_now.date() + dt.timedelta(days=day_offset)
        for value in sorted(config.daily_times):
            hour, minute = map(int, value.split(":")); naive = dt.datetime.combine(day, dt.time(hour, minute))
            # Spring DST gaps advance to the first valid local minute; autumn ambiguity uses fold=0.
            while not _valid_local(naive, zone): naive += dt.timedelta(minutes=1)
            candidate = naive.replace(tzinfo=zone, fold=0)
            if candidate > local_now: return candidate
    raise RuntimeError("Nelze vypočítat další synchronizaci")


def next_backup(now: dt.datetime, config: RuntimeConfig) -> dt.datetime | None:
    if config.backup_frequency == "disabled": return None
    zone = ZoneInfo(config.timezone); local_now = now.astimezone(zone); hour, minute = map(int, config.backup_time.split(":"))
    for offset in range(9):
        day = local_now.date() + dt.timedelta(days=offset)
        if config.backup_frequency == "weekly" and day.weekday() != 6: continue
        naive = dt.datetime.combine(day, dt.time(hour, minute))
        while not _valid_local(naive, zone): naive += dt.timedelta(minutes=1)
        candidate = naive.replace(tzinfo=zone, fold=0)
        if candidate > local_now: return candidate
    return None


def set_admin_password(db, password: str, actor: str = "admin") -> None:
    """Persist a salted hash of the admin password (overrides the bootstrap secret). Never audits the value."""
    from .auth import HASH_SETTING_KEY, hash_password
    value = json.dumps(hash_password(password))
    row = db.get(AppSetting, HASH_SETTING_KEY)
    if row: row.value_json = value
    else: db.add(AppSetting(key=HASH_SETTING_KEY, value_json=value))
    db.add(AuditLog(actor=actor, action="admin_password_changed", details_json="{}"))


def verify_admin_password(db, password: str, bootstrap_password: str) -> bool:
    """Stored hash (set in the UI / CLI) wins; otherwise the bootstrap secret from the environment."""
    import hmac

    from .auth import HASH_SETTING_KEY, verify_password
    stored = get_setting(db, HASH_SETTING_KEY)
    if stored:
        return verify_password(password, stored)
    return bool(bootstrap_password) and hmac.compare_digest(password, bootstrap_password)
