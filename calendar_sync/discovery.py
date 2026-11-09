from __future__ import annotations

import datetime as dt
import re
import unicodedata

from sqlalchemy import select

from .models import DiscoveredTechnician

_HEADERS = {"technik", "technici", "jmeno", "jmeno technika", "name", "employee", "zamestnanec"}


def clean_name(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalized_name(value: object) -> str:
    cleaned = clean_name(value)
    folded = "".join(c for c in unicodedata.normalize("NFKD", cleaned) if not unicodedata.combining(c))
    return folded.casefold()


def is_candidate(value: object) -> bool:
    display = clean_name(value)
    key = normalized_name(display).rstrip(":")
    return bool(display) and not normalized_name(display).startswith("legenda:") and key not in _HEADERS


def record_discovered(db, issues: list[dict]) -> None:
    now = dt.datetime.now(dt.UTC)
    for issue in issues:
        if issue.get("code") != "unmapped_technician" or not is_candidate(issue.get("name")):
            continue
        display = clean_name(issue["name"])
        key = normalized_name(display)
        item = db.scalar(select(DiscoveredTechnician).where(DiscoveredTechnician.normalized_name == key))
        if item:
            item.last_seen_at = now
        else:
            db.add(DiscoveredTechnician(normalized_name=key, display_name=display, last_seen_at=now))
    db.commit()
