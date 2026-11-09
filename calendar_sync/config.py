from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def secret(name: str, default: str = "") -> str:
    file_name = os.getenv(f"{name}_FILE", "")
    if file_name:
        return Path(file_name).read_text(encoding="utf-8").strip()
    return os.getenv(name, default)


def as_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    database_url: str
    source_file_id: str
    admin_password: str
    session_secret: str
    public_base_url: str
    cookie_secure: bool
    poll_seconds: int
    stability_seconds: int
    delete_enabled: bool
    oauth_client_file: Path
    oauth_token_file: Path
    log_level: str

    @classmethod
    def from_env(cls) -> Settings:
        data = Path(os.getenv("DATA_DIR", "data"))
        return cls(
            data_dir=data,
            database_url=os.getenv("DATABASE_URL", f"sqlite:///{data / 'calendar-sync.db'}"),
            source_file_id=os.getenv("GOOGLE_DRIVE_FILE_ID", ""),
            admin_password=secret("ADMIN_PASSWORD"),
            session_secret=secret("SESSION_SECRET"),
            public_base_url=os.getenv("PUBLIC_BASE_URL", "http://localhost:8080").rstrip("/"),
            cookie_secure=as_bool(os.getenv("COOKIE_SECURE", "true")),
            poll_seconds=int(os.getenv("POLL_SECONDS", "120")),
            stability_seconds=int(os.getenv("STABILITY_SECONDS", "60")),
            delete_enabled=as_bool(os.getenv("DELETE_ENABLED", "false")),
            oauth_client_file=Path(os.getenv("GOOGLE_OAUTH_CLIENT_FILE", "secrets/google_oauth_client.json")),
            oauth_token_file=Path(os.getenv("GOOGLE_OAUTH_TOKEN_FILE", str(data / "google-token.json"))),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
        )

    def validate_runtime(self) -> list[str]:
        errors = []
        if not self.admin_password:
            errors.append("ADMIN_PASSWORD or ADMIN_PASSWORD_FILE is required")
        if len(self.session_secret) < 32:
            errors.append("SESSION_SECRET must contain at least 32 characters")
        return errors
