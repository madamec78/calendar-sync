from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .parser import Event


@dataclass
class Plan:
    creates: list[Event]
    updates: list[tuple[dict, Event]]
    unchanged: list[tuple[dict, Event]]
    missing: list[dict]
    foreign: list[dict]


def private(event: dict) -> dict:
    return ((event.get("extendedProperties") or {}).get("private") or {})


def canonical_google(event: dict) -> dict:
    out = {key: event.get(key) for key in ("summary", "description", "start", "end", "extendedProperties")}
    out["description"] = out.get("description") or ""
    if event.get("location"): out["location"] = event["location"]
    if event.get("colorId"): out["colorId"] = event["colorId"]
    return out


# Volatile provenance markers change on *every* Drive edit; comparing them would rewrite every event
# (API quota, event "updated" churn on subscribers' devices) whenever anyone touches the XLSX.
VOLATILE_PRIVATE_KEYS = ("source_version", "source_hash", "sync_mark")


def comparable(body: dict) -> dict:
    out = dict(body)
    props = dict((out.get("extendedProperties") or {}).get("private") or {})
    for key in VOLATILE_PRIVATE_KEYS:
        props.pop(key, None)
    out["extendedProperties"] = {"private": props}
    return out


def plan(events: Iterable[Event], existing: Iterable[dict], file_id: str, technician: str, canonical_id: str | None = None) -> Plan:
    ours, foreign = [], []
    for item in existing:
        props = private(item)
        owner_matches = props.get("canonical_technician_id") == canonical_id if props.get("canonical_technician_id") and canonical_id else props.get("technician_name") == technician
        if props.get("source_file_id") == file_id and owner_matches:
            ours.append(item)
        else:
            foreign.append(item)
    by_key = {private(item).get("source_key"): item for item in ours}
    # Migration guard: v1 keys contained row/cell references. Match a single legacy event
    # by stable semantic fields so moving rows upgrades it instead of duplicating it.
    legacy = [item for item in ours if "|" in str(private(item).get("source_key", "")) and not str(private(item).get("source_key", "")).startswith("v2|")]
    creates = []; updates = []; unchanged = []; seen = set()
    for event in events:
        old = by_key.get(event.source_key)
        if not old:
            candidates = [item for item in legacy if item.get("summary") == event.summary and item.get("start") == event.start and item.get("end") == event.end and item.get("id") not in seen]
            old = candidates[0] if len(candidates) == 1 else None
        if not old:
            creates.append(event); continue
        seen.add(old["id"])
        if comparable(event.google_body()) == comparable(canonical_google(old)): unchanged.append((old, event))
        else: updates.append((old, event))
    return Plan(creates, updates, unchanged, [x for x in ours if x.get("id") not in seen], foreign)


def next_stale_state(previous_streak: int | None, missing: bool, *, already_counted: bool = False) -> tuple[int, bool]:
    """Two consecutive stable, *distinct* snapshots are required before an owned item is pending stale.

    ``already_counted`` means the current snapshot was already counted for this event, so the
    streak must stay where it is (re-running a sync over the same Drive version proves nothing).
    """
    previous = previous_streak or 0
    if not missing:
        return 0, False
    streak = previous if already_counted else previous + 1
    return streak, streak >= 2
