"""Backend synchronization invariants; no live network or device access."""
from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from tools.poll_alarms import AlarmPoller, PollError


def event(seq, alarm="camera", active=True, transition="occurred", stream="stream1"):
    return {
        "event_id": f"{stream}-{seq}", "alarm_id": alarm, "sequence": seq,
        "category": "camera", "code": "CAMERA_UNAVAILABLE", "severity": "P1",
        "transition": transition, "occurred_at": "2026-11-03T00:00:00+00:00",
        "message": "相机不可用", "component": "camera", "resource": "default",
        "active": active, "count": 1, "task_id": None, "execution_id": None,
        "diagnostic_id": "d1",
    }


def page(items=(), *, cursor="tail", more=False, watermark=10, stream="stream1", monitor=None):
    return {
        "protocol_version": 1, "device_id": "device1", "stream_id": stream,
        "boot_id": "boot1", "items": list(items), "next_cursor": cursor,
        "has_more": more, "watermark": watermark, "retention_start_sequence": 1,
        "events_cursor": f"watermark-{watermark}",
        "monitoring": monitor or {"store": {"status": "ok"}},
    }


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, headers, timeout):
        self.calls.append((parse_qs(urlsplit(url).query), headers, timeout))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result if isinstance(result, tuple) else (200, copy.deepcopy(result))


class AlarmPollerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "backend.sqlite3"
        self.now = 1000.0
        self.pollers = []

    def tearDown(self):
        for poller in self.pollers:
            poller.close()
        self.tmp.cleanup()

    def make(self, transport):
        client = AlarmPoller("https://device.example", self.db_path, "test-secret", transport=transport, clock=lambda: self.now)
        self.pollers.append(client)
        return client

    def rows(self, poller, table):
        return [dict(row) for row in poller.db.execute(f"SELECT * FROM {table}")]

    def test_initial_snapshot_and_history_preserve_transient_fault_and_recovery(self):
        transport = FakeTransport(
            page([event(5, "persistent")], cursor=None),
            page([event(1), event(2, active=False, transition="recovered")], cursor="p2", more=True),
            page([event(5, "persistent")], cursor="tail"),
        )
        client = self.make(transport)
        self.assertTrue(client.poll_once())
        self.assertEqual([row["sequence"] for row in self.rows(client, "events")], [1, 2, 5])
        self.assertEqual([(row["alarm_id"], row["active"]) for row in self.rows(client, "alarm_state")], [("persistent", 1)])
        self.assertEqual(client._get("cursor"), "tail")
        self.assertNotIn("cursor", transport.calls[1][0])
        self.assertEqual(transport.calls[2][0]["cursor"], ["p2"])
        self.assertEqual(transport.calls[0][1]["Authorization"], "Bearer test-secret")
        self.assertEqual(transport.calls[0][2], 3.0)

    def test_restart_uses_durable_cursor_and_empty_page_updates_monitoring(self):
        first = self.make(FakeTransport(page(cursor=None), page(cursor="c7")))
        self.assertTrue(first.poll_once())
        first.close()
        self.pollers.remove(first)
        monitor = {"task_worker": {"status": "dead"}}
        transport = FakeTransport(page(cursor="c7", monitor=monitor))
        second = self.make(transport)
        self.assertTrue(second.poll_once())
        self.assertEqual(transport.calls[0][0], {"mode": ["events"], "limit": ["100"], "cursor": ["c7"]})
        self.assertEqual(second.health()["monitoring"], monitor)

    def test_failure_preserves_cursor_and_alarms_and_offline_is_backend_origin(self):
        transport = FakeTransport(page([event(5)], cursor=None), page(cursor="old"), PollError("network"))
        client = self.make(transport)
        self.assertTrue(client.poll_once())
        self.now += 61
        self.assertFalse(client.poll_once())
        self.assertEqual(client._get("cursor"), "old")
        self.assertEqual(self.rows(client, "alarm_state")[0]["active"], 1)
        self.assertTrue(client.health()["communication_alarm"])
        self.assertEqual(client.health()["origin"], "business_backend")
        transport.responses.append(page(cursor="old"))
        self.assertTrue(client.poll_once())
        self.assertFalse(client.health()["communication_alarm"])

    def test_snapshot_applies_only_after_every_page_and_retains_known_alarms_on_failure(self):
        transport = FakeTransport(page([event(5, "old")], cursor=None), page(cursor="old"))
        client = self.make(transport)
        self.assertTrue(client.poll_once())
        transport.responses.extend([
            (410, {"detail": {"resync_reason": "retention", "stream_id": "stream1"}}),
            page([event(11, "new")], cursor="s2", more=True, watermark=20),
            PollError("network"),
        ])
        self.assertFalse(client.poll_once())
        self.assertEqual([(r["alarm_id"], r["active"]) for r in self.rows(client, "alarm_state")], [("old", 1)])
        self.assertEqual(client._get("cursor"), "old")
        self.assertEqual(len(self.rows(client, "history_gaps")), 1)
        transport.responses.extend([page([event(11, "new")], cursor=None, watermark=20), page([event(11, "new")], watermark=20)])
        self.assertTrue(client.poll_once())
        state = {r["alarm_id"]: r["active"] for r in self.rows(client, "alarm_state")}
        self.assertEqual(state, {"old": 0, "new": 1})

    def test_snapshot_version_guard_rejects_old_recovery_during_replay(self):
        client = self.make(FakeTransport(
            page([event(8, transition="updated")], cursor=None),
            page([event(1), event(3, active=False, transition="recovered"), event(8, transition="updated")]),
            page([event(11, active=False, transition="recovered")], watermark=11, cursor="tail-11"),
        ))
        self.assertTrue(client.poll_once())
        self.assertEqual(self.rows(client, "alarm_state")[0]["active"], 1)
        self.assertEqual(self.rows(client, "alarm_state")[0]["sequence"], 8)
        self.assertTrue(client.poll_once())
        self.assertEqual(self.rows(client, "alarm_state")[0]["active"], 0)

    def test_stream_rebuild_keeps_history_and_records_gap(self):
        transport = FakeTransport(page([event(5)], cursor=None), page([event(5)]))
        client = self.make(transport)
        self.assertTrue(client.poll_once())
        transport.responses.extend([
            (410, {"detail": {"resync_reason": "stream_changed", "stream_id": "stream2", "retention_start_sequence": 1}}),
            page([event(1, "replacement", stream="stream2")], stream="stream2", cursor=None),
            page([event(1, "replacement", stream="stream2")], stream="stream2"),
        ])
        self.assertTrue(client.poll_once())
        self.assertEqual(len(self.rows(client, "events")), 2)
        self.assertEqual(client._get("stream_id"), "stream2")
        self.assertEqual([r["alarm_id"] for r in self.rows(client, "alarm_state")], ["replacement"])
        self.assertEqual(self.rows(client, "history_gaps")[0]["reason"], "stream_changed")

    def test_replay_deduplicates_after_resync(self):
        transport = FakeTransport(page([event(5)], cursor=None), page([event(5)]))
        client = self.make(transport)
        self.assertTrue(client.poll_once())
        transport.responses.extend([(410, {"detail": {"resync_reason": "expired"}}), page([event(5)], cursor=None), page([event(5)])])
        self.assertTrue(client.poll_once())
        self.assertEqual(len(self.rows(client, "events")), 1)

    def test_failed_cursor_commit_rolls_back_events_and_state(self):
        transport = FakeTransport(page(cursor=None), page(cursor="old"), page([event(11)], cursor="new", watermark=11))
        client = self.make(transport)
        self.assertTrue(client.poll_once())
        original = client._put

        def fail_cursor(key, value):
            if key == "cursor":
                raise sqlite3.OperationalError("injected database write error")
            return original(key, value)

        with patch.object(client, "_put", side_effect=fail_cursor):
            with self.assertRaises(sqlite3.OperationalError):
                client.poll_once()
        self.assertEqual(client._get("cursor"), "old")
        self.assertEqual(self.rows(client, "events"), [])
        self.assertEqual(self.rows(client, "alarm_state"), [])

    def test_schema_error_and_http_failures_do_not_clear_state(self):
        transport = FakeTransport(page([event(5)], cursor=None), page(cursor="old"))
        client = self.make(transport)
        self.assertTrue(client.poll_once())
        for status, kind in ((401, "authentication"), (403, "authorization"), (503, "service_unavailable"), (500, "http")):
            transport.responses.append((status, {}))
            self.assertFalse(client.poll_once())
            self.assertEqual(client.health()["last_error_kind"], kind)
        malformed = page()
        malformed["items"] = "not an event list"
        transport.responses.append(malformed)
        self.now += 61
        self.assertFalse(client.poll_once())
        self.assertEqual(client.health()["last_error_kind"], "schema")
        self.assertTrue(client.health()["communication_alarm"])
        self.assertEqual(client._get("cursor"), "old")
        self.assertEqual(self.rows(client, "alarm_state")[0]["active"], 1)

    def test_mismatched_snapshot_watermark_does_not_publish_partial_state(self):
        client = self.make(FakeTransport(
            page([event(1)], cursor="s2", more=True, watermark=10),
            page([event(11, "other")], cursor=None, watermark=11),
        ))
        self.assertFalse(client.poll_once())
        self.assertEqual(self.rows(client, "alarm_state"), [])
        self.assertIsNone(client._get("initialized"))

    def test_snapshot_cannot_clear_state_newer_than_watermark(self):
        client = self.make(FakeTransport(page(cursor=None), page()))
        self.assertTrue(client.poll_once())
        with client.db:
            client._apply_state(event(25), "stream1")
            client._put("resync_required", "1")
        client.transport.responses.extend([page(cursor=None, watermark=20), page(watermark=20)])
        self.assertTrue(client.poll_once())
        self.assertEqual(self.rows(client, "alarm_state")[0]["active"], 1)

    def test_requires_tls_and_credential_and_nonoverlap(self):
        with self.assertRaises(ValueError):
            AlarmPoller("http://localhost:8000", ":memory:", "secret")
        with self.assertRaises(ValueError):
            AlarmPoller("https://device.example", ":memory:", "")
        client = self.make(FakeTransport())
        client._poll_lock.acquire()
        try:
            with self.assertRaises(RuntimeError):
                client.poll_once()
        finally:
            client._poll_lock.release()

    def test_restart_between_snapshot_and_replay_detects_expired_recovery(self):
        from workflow.alarm_store import AlarmStore
        now = [1000.0]
        store = AlarmStore(Path(self.tmp.name) / "device.db", device_id="device1",
                           retention_days=1, clock=lambda: now[0])
        args = dict(code="CAMERA_UNAVAILABLE", category="camera", severity="P1",
                    message="相机不可用", component="camera")
        store.record(**args)
        def transport(url, headers, timeout):
            query = parse_qs(urlsplit(url).query)
            result = store.query(mode=query["mode"][0], cursor=query.get("cursor", [None])[0],
                                 limit=int(query["limit"][0]))
            result["monitoring"] = {}
            return 200, result
        first = self.make(transport)
        first._snapshot()
        first.close()
        self.pollers.remove(first)
        store.record(**args, recovery=True)
        now[0] += 2 * 86400
        store.prune()
        restarted = self.make(transport)
        self.assertTrue(restarted.poll_once())
        self.assertFalse(restarted.db.execute("SELECT 1 FROM alarm_state WHERE active=1").fetchone())
        self.assertEqual(self.rows(restarted, "history_gaps")[0]["reason"], "retention_expired_after_snapshot")


if __name__ == "__main__":
    unittest.main()
