from __future__ import annotations

import datetime as dt
from pathlib import Path

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker


class Base(DeclarativeBase):
    pass


class Technician(Base):
    __tablename__ = "technicians"
    id: Mapped[int] = mapped_column(primary_key=True)
    canonical_id: Mapped[str | None] = mapped_column(String(100), unique=True, nullable=True)
    workbook_name: Mapped[str] = mapped_column(String(250), unique=True)
    aliases_json: Mapped[str] = mapped_column(Text, default="[]")
    email: Mapped[str] = mapped_column(String(320), default="")
    calendar_id: Mapped[str | None] = mapped_column(String(500), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    onboarding_status: Mapped[str] = mapped_column(String(40), default="new")
    onboarding_link: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_sync_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC))


class DiscoveredTechnician(Base):
    __tablename__ = "discovered_technicians"
    id: Mapped[int] = mapped_column(primary_key=True)
    normalized_name: Mapped[str] = mapped_column(String(250), unique=True)
    display_name: Mapped[str] = mapped_column(String(250))
    ignored: Mapped[bool] = mapped_column(Boolean, default=False)
    first_seen_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC))
    last_seen_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC))


class Snapshot(Base):
    __tablename__ = "snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(300), unique=True)
    drive_version: Mapped[str] = mapped_column(String(100))
    md5: Mapped[str] = mapped_column(String(64))
    stable: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC))


class OwnedEvent(Base):
    __tablename__ = "owned_events"
    __table_args__ = (UniqueConstraint("technician_id", "source_key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    technician_id: Mapped[int] = mapped_column(ForeignKey("technicians.id", ondelete="CASCADE"))
    source_key: Mapped[str] = mapped_column(String(700))
    google_event_id: Mapped[str] = mapped_column(String(300))
    missing_streak: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    pending_stale: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    last_seen_fingerprint: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # Fingerprint of the Drive snapshot in which the event was last counted as missing. A repeated run
    # over the same snapshot must never advance the streak (two *distinct* snapshots are required).
    missing_fingerprint: Mapped[str | None] = mapped_column(String(300), nullable=True)
    technician: Mapped[Technician] = relationship()


class AppSetting(Base):
    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value_json: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC), onupdate=lambda: dt.datetime.now(dt.UTC))


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC))
    actor: Mapped[str] = mapped_column(String(100), default="admin")
    action: Mapped[str] = mapped_column(String(100))
    details_json: Mapped[str] = mapped_column(Text, default="{}")


class SyncRun(Base):
    __tablename__ = "sync_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC))
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    mode: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(30), default="running")
    fingerprint: Mapped[str | None] = mapped_column(String(300), nullable=True)
    summary_json: Mapped[str] = mapped_column(Text, default="{}")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


def make_session_factory(database_url: str):
    if database_url.startswith("sqlite") and not database_url.endswith(":memory:"):
        raw_path = database_url.split("///", 1)[-1]
        Path(raw_path).parent.mkdir(parents=True, exist_ok=True)
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    engine = create_engine(database_url, connect_args=connect_args)
    if database_url.startswith("sqlite"):
        from sqlalchemy import event as sa_event

        @sa_event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _record):  # pragma: no cover - trivial
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=10000")
            cursor.close()
    return engine, sessionmaker(engine, expire_on_commit=False)


def init_db(engine) -> None:
    """Create missing tables and perform additive migrations without replacing data."""
    Base.metadata.create_all(engine)
    if engine.dialect.name == "sqlite":
        from sqlalchemy import inspect, text
        columns = {x["name"] for x in inspect(engine).get_columns("technicians")}
        owned_columns = {x["name"] for x in inspect(engine).get_columns("owned_events")}
        with engine.begin() as connection:
            if "missing_fingerprint" not in owned_columns:
                connection.execute(text("ALTER TABLE owned_events ADD COLUMN missing_fingerprint VARCHAR(300)"))
            if "canonical_id" not in columns:
                connection.execute(text("ALTER TABLE technicians ADD COLUMN canonical_id VARCHAR(100)"))
            if "aliases_json" not in columns:
                connection.execute(text("ALTER TABLE technicians ADD COLUMN aliases_json TEXT NOT NULL DEFAULT '[]'"))
            # Older databases allowed these values to remain NULL. Normalize
            # them before reconciliation so stale counting is deterministic.
            connection.execute(text("UPDATE owned_events SET missing_streak = 0 WHERE missing_streak IS NULL"))
            connection.execute(text("UPDATE owned_events SET pending_stale = 0 WHERE pending_stale IS NULL"))
