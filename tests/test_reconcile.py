from calendar_sync.parser import Event
from calendar_sync.reconcile import next_stale_state, plan


def event(key="k1"):
    return Event("Test Technik", key, "full summary", "", None, "3", {"date": "2026-09-21"}, {"date": "2026-09-22"},
                 {"source_file_id": "file-1", "source_key": key, "technician_name": "Test Technik"})


def google_event(item: Event, event_id="g1"):
    return {"id": event_id, **item.google_body()}


def test_reconcile_idempotent_and_foreign_untouched():
    ours = google_event(event())
    foreign = {"id": "foreign", "summary": "manual", "extendedProperties": {"private": {}}}
    result = plan([event()], [ours, foreign], "file-1", "Test Technik")
    assert len(result.unchanged) == 1 and not result.creates and not result.updates
    assert [x["id"] for x in result.foreign] == ["foreign"]


def test_reconcile_detects_update_and_missing():
    old = google_event(event("old"))
    result = plan([event("new")], [old], "file-1", "Test Technik")
    assert len(result.creates) == 1 and [x["id"] for x in result.missing] == ["g1"]


def test_stale_requires_two_consecutive_stable_snapshots():
    streak, pending = next_stale_state(0, True); assert (streak, pending) == (1, False)
    streak, pending = next_stale_state(streak, True); assert (streak, pending) == (2, True)
    assert next_stale_state(streak, False) == (0, False)


def test_new_unflushed_mapping_starts_stale_counter_at_zero():
    assert next_stale_state(None, True) == (1, False)
