"""Synchronization invariants: no lost transient alarms or inconsistent snapshots."""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from workflow.alarm_store import AlarmQueryError, AlarmStore


@pytest.fixture
def store(tmp_path):
    return AlarmStore(tmp_path / "alarms.sqlite3", device_id="industrial-1")


def record(store, code="CAMERA_FAILED", **kwargs):
    return store.record(code=code, category="camera", severity="P1", message="相机故障",
                        component="camera", **kwargs)


def test_transient_failure_recovery_and_history_from_oldest(store):
    alarm_id = record(store)
    record(store, recovery=True)
    events = store.query()["items"]
    assert [e["transition"] for e in events] == ["occurred", "recovered"]
    assert {e["alarm_id"] for e in events} == {alarm_id}
    assert store.query(mode="snapshot")["items"] == []
    assert store.query(cursor=store.query()["next_cursor"])["items"] == []


def test_repetition_groups_counts_and_new_incident_gets_new_id(store):
    first = record(store)
    assert record(store) == first
    assert store.query()["items"][-1]["count"] == 2
    record(store, recovery=True)
    assert record(store) != first
    assert len(store.query(mode="snapshot")["items"]) == 1


def test_restart_retains_stream_sequence_and_cursor(store):
    record(store)
    page = store.query()
    restarted = AlarmStore(store.path)
    assert restarted.stream_id == store.stream_id
    assert restarted.boot_id != store.boot_id
    assert restarted.device_id == store.device_id
    record(restarted, "STAGE_FAILED")
    assert [e["sequence"] for e in restarted.query(cursor=page["next_cursor"])["items"]] == [2]


def test_state_and_event_are_atomic_on_database_failure(store):
    with sqlite3.connect(store.path) as db:
        db.execute("CREATE TRIGGER fail_state BEFORE INSERT ON alarms BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(sqlite3.IntegrityError):
        record(store)
    assert store.query()["items"] == []
    assert store.query(mode="snapshot")["items"] == []


def test_paging_advances_only_returned_events(store):
    for i in range(7):
        record(store, str(i))
    items, cursor = [], None
    while True:
        page = store.query(cursor=cursor, limit=2)
        items.extend(page["items"])
        cursor = page["next_cursor"]
        if not page["has_more"]:
            break
    assert [e["sequence"] for e in items] == list(range(1, 8))
    assert store.query(cursor=cursor)["items"] == []


def test_snapshot_is_frozen_across_recovery_and_new_fault(store):
    for code in ("a", "b", "c"):
        record(store, code)
    initial = store.query(mode="snapshot", limit=1)
    for code in ("a", "b", "c"):
        record(store, code, recovery=True)
    record(store, "new")
    items, page = list(initial["items"]), initial
    while page["has_more"]:
        page = store.query(mode="snapshot", cursor=page["next_cursor"], limit=1)
        assert page["watermark"] == 3
        items.extend(page["items"])
    assert {e["code"] for e in items} == {"a", "b", "c"}
    assert len(store.query(cursor=page["events_cursor"])["items"]) == 4


def test_snapshot_expiration_requires_resync(tmp_path):
    now = [100.0]
    store = AlarmStore(tmp_path / "alarms.db", clock=lambda: now[0], snapshot_ttl=10)
    record(store, "a")
    record(store, "b")
    page = store.query(mode="snapshot", limit=1)
    now[0] += 11
    with pytest.raises(AlarmQueryError) as error:
        store.query(mode="snapshot", cursor=page["next_cursor"])
    assert error.value.status == 410
    assert error.value.detail["resync_reason"] == "snapshot_expired"


def test_retention_keeps_active_state_and_permanent_fact_dedup(tmp_path):
    now = [100.0]
    store = AlarmStore(tmp_path / "alarms.db", clock=lambda: now[0], retention_days=1)
    old = store.query()["next_cursor"]
    record(store, "active")
    fact_id = record(store, "task", fact=True, occurrence_key="execution1:failed")
    now[0] += 2 * 86400
    store.prune()
    assert store.query()["items"] == []
    assert len(store.query(mode="snapshot")["items"]) == 1
    assert record(store, "task", fact=True, occurrence_key="execution1:failed") == fact_id
    assert store.query()["items"] == []
    with pytest.raises(AlarmQueryError) as error:
        store.query(cursor=old)
    assert error.value.status == 410
    assert error.value.detail["retention_start_sequence"] == 3


def test_task_facts_are_distinct_per_execution_and_not_active(store):
    record(store, "TASK_FAILED", fact=True, occurrence_key="execution1:failed", execution_id="execution1")
    record(store, "TASK_FAILED", fact=True, occurrence_key="execution2:failed", execution_id="execution2")
    assert len(store.query()["items"]) == 2
    assert store.query(mode="snapshot")["items"] == []


def test_query_is_read_only_for_business_state(store):
    record(store)
    with sqlite3.connect(store.path) as db:
        before = list(db.iterdump())
    for _ in range(3):
        store.query(mode="snapshot", limit=1)
        store.query()
    with sqlite3.connect(store.path) as db:
        assert list(db.iterdump()) == before


@pytest.mark.parametrize("cursor", ["garbage", "x.y.z", "!" * 4100, "e30.bad"])
def test_bad_cursor_is_400(store, cursor):
    with pytest.raises(AlarmQueryError) as error:
        store.query(cursor=cursor)
    assert error.value.status == 400


def test_restore_rotation_expires_old_stream_cursor(store):
    from workflow.alarm_maintenance import rotate_stream
    record(store)
    old = store.query()["next_cursor"]
    new_stream = rotate_stream(store.path)
    replacement = AlarmStore(store.path)
    assert replacement.stream_id == new_stream != store.stream_id
    with pytest.raises(AlarmQueryError) as error:
        replacement.query(cursor=old)
    assert error.value.detail["resync_reason"] == "stream_changed"
    event = replacement.query()["items"][0]
    assert event["event_id"].startswith(new_stream + ":")


def test_concurrent_writers_preserve_unique_order_and_state(store):
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda i: record(store, str(i)), range(40)))
    items = store.query()["items"]
    assert [e["sequence"] for e in items] == list(range(1, 41))
    assert len(store.query(mode="snapshot")["items"]) == 40


def test_reconciled_time_does_not_expire_newly_recorded_history(store):
    record(store, fact=True, occurrence_key="old", occurred_at="2020-01-01T00:00:00+00:00")
    store.prune()
    item = store.query()["items"][0]
    assert item["occurred_at"].startswith("2020-")
    assert item["recorded_at"] != item["occurred_at"]
