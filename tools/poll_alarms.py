"""Reference business-backend consumer for GET /api/alarms (stdlib only).

Run one process per device. Its SQLite database demonstrates transaction and
deduplication boundaries; integrate these boundaries into the real backend's
database and scheduler rather than launching a second consumer for each type.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable


logger = logging.getLogger(__name__)


class PollError(RuntimeError):
    """An intentionally public error; raw HTTP bodies/credentials are omitted."""

    def __init__(self, kind: str, status: int | None = None, detail: dict | None = None):
        super().__init__(kind)
        self.kind = kind
        self.status = status
        self.detail = detail or {}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward a service credential to a redirected host or HTTP URL.
        return None


class AlarmPoller:
    """Single-device, non-overlapping consumer with injected transport/time.

    ``transport(url, headers, timeout)`` returns ``(http_status, json_object)``.
    ``poll_once`` immediately drains all pages, then returns. Failed requests
    preserve the previously committed event cursor and known device alarms.
    """

    def __init__(
        self,
        base_url: str,
        database: str | Path,
        token: str,
        *,
        allow_http: bool = False,
        transport: Callable[[str, dict, float], tuple[int, Any]] | None = None,
        clock: Callable[[], float] = time.time,
        timeout: float = 3.0,
        unavailable_after: float = 60.0,
        page_size: int = 100,
    ) -> None:
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme not in ({"https", "http"} if allow_http else {"https"}):
            raise ValueError("HTTPS required; --allow-http is only for controlled local testing")
        if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("base URL must contain a host and no credentials/query/fragment")
        if not token.strip():
            raise ValueError("COLONY_ALARM_TOKEN is required")
        if not 1 <= page_size <= 500 or timeout <= 0 or unavailable_after <= 0:
            raise ValueError("invalid polling parameters")
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.clock = clock
        self.timeout = timeout
        self.unavailable_after = unavailable_after
        self.page_size = page_size
        self.transport = transport or self._http_get
        self._poll_lock = threading.Lock()
        self.db = sqlite3.connect(str(database))
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                device_id TEXT NOT NULL, stream_id TEXT NOT NULL, event_id TEXT NOT NULL,
                sequence INTEGER NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(device_id, stream_id, event_id),
                UNIQUE(device_id, stream_id, sequence)
            );
            CREATE TABLE IF NOT EXISTS alarm_state (
                alarm_id TEXT PRIMARY KEY, stream_id TEXT NOT NULL,
                sequence INTEGER NOT NULL, active INTEGER NOT NULL, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS history_gaps (
                id INTEGER PRIMARY KEY, detected_at REAL NOT NULL, reason TEXT NOT NULL,
                previous_stream_id TEXT, new_stream_id TEXT, retention_start_sequence INTEGER
            );
        """)
        with self.db:
            if self._get("base_url") not in (None, self.base_url):
                raise ValueError("use one consumer database per device base URL")
            self._put("base_url", self.base_url)
            if self._get("monitoring_started_at") is None:
                self._put("monitoring_started_at", str(self.clock()))

    def close(self) -> None:
        self.db.close()

    def _get(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def _put(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES(?,?)", (key, value))

    @staticmethod
    def _http_get(url: str, headers: dict, timeout: float) -> tuple[int, Any]:
        request = urllib.request.Request(url, headers=headers, method="GET")
        opener = urllib.request.build_opener(_NoRedirect())
        try:
            response = opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            response = exc
        except (OSError, urllib.error.URLError) as exc:
            raise PollError("network") from exc
        with response:
            # Limit a faulty peer's payload rather than exhausting backend memory.
            raw = response.read(8 * 1024 * 1024 + 1)
            status = response.code
        if len(raw) > 8 * 1024 * 1024:
            raise PollError("schema", status)
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            if status != 200:
                payload = {}
            else:
                raise PollError("schema", status) from exc
        return status, payload

    @staticmethod
    def _integer(value: Any) -> bool:
        return isinstance(value, int) and not isinstance(value, bool)

    def _validate_page(self, page: Any, mode: str) -> dict:
        if not isinstance(page, dict) or type(page.get("protocol_version")) is not int or page["protocol_version"] != 1:
            raise PollError("schema")
        for name in ("device_id", "stream_id", "boot_id"):
            if not isinstance(page.get(name), str) or not page[name]:
                raise PollError("schema")
        if self._get("device_id") not in (None, page["device_id"]):
            raise PollError("wrong_device")
        if not isinstance(page.get("items"), list) or len(page["items"]) > self.page_size:
            raise PollError("schema")
        if type(page.get("has_more")) is not bool or not isinstance(page.get("monitoring"), dict):
            raise PollError("schema")
        for field in ("watermark", "retention_start_sequence"):
            if not self._integer(page.get(field)) or page[field] < 0:
                raise PollError("schema")
        if mode == "events" or page["has_more"]:
            if not isinstance(page.get("next_cursor"), str) or not page["next_cursor"]:
                raise PollError("schema")
        if mode == "snapshot" and not isinstance(page.get("events_cursor"), str):
            raise PollError("schema")
        previous = -1
        ids = set()
        for item in page["items"]:
            if not isinstance(item, dict):
                raise PollError("schema")
            for field in ("event_id", "alarm_id", "category", "code", "message", "component", "occurred_at"):
                if not isinstance(item.get(field), str) or not item[field]:
                    raise PollError("schema")
            seq = item.get("sequence")
            if not self._integer(seq) or not 0 < seq <= page["watermark"]:
                raise PollError("schema")
            if mode == "events" and seq <= previous:
                raise PollError("schema")
            previous = seq
            if item["event_id"] in ids:
                raise PollError("schema")
            ids.add(item["event_id"])
            if item.get("severity") not in ("P0", "P1", "P2", "INFO"):
                raise PollError("schema")
            if item.get("transition") not in ("occurred", "updated", "recovered", "recorded"):
                raise PollError("schema")
            if type(item.get("active")) is not bool or (mode == "snapshot" and not item["active"]):
                raise PollError("schema")
            if not self._integer(item.get("count")) or item["count"] < 1:
                raise PollError("schema")
            if item["active"] != (item["transition"] in ("occurred", "updated")):
                raise PollError("schema")
        if page["has_more"] and not page["items"]:
            raise PollError("schema")
        return page

    def _fetch(self, mode: str, cursor: str | None) -> dict:
        params: dict[str, str | int] = {"mode": mode, "limit": self.page_size}
        if cursor is not None:
            params["cursor"] = cursor
        url = self.base_url + "/api/alarms?" + urllib.parse.urlencode(params)
        status, payload = self.transport(
            url, {"Authorization": "Bearer " + self._token, "Accept": "application/json"}, self.timeout
        )
        if status != 200:
            kind = {401: "authentication", 403: "authorization", 410: "resync", 503: "service_unavailable"}.get(status, "http")
            detail = payload.get("detail", {}) if isinstance(payload, dict) else {}
            raise PollError(kind, status, detail if isinstance(detail, dict) else {})
        page = self._validate_page(payload, mode)
        with self.db:
            self._put("last_valid_response_at", str(self.clock()))
            self._put("monitoring", json.dumps(page["monitoring"], ensure_ascii=False))
            self._put("boot_id", page["boot_id"])
        return page

    def _require_resync(self, detail: dict, *, history_gap: bool = True) -> None:
        with self.db:
            if history_gap and self._get("resync_required") != "1":
                reason = str(detail.get("resync_reason") or "stream_changed")[:100]
                self.db.execute(
                    "INSERT INTO history_gaps(detected_at,reason,previous_stream_id,new_stream_id,retention_start_sequence) VALUES(?,?,?,?,?)",
                    (self.clock(), reason, self._get("stream_id"), detail.get("stream_id"), detail.get("retention_start_sequence")),
                )
            self._put("resync_required", "1")

    def _apply_state(self, item: dict, stream_id: str) -> None:
        self.db.execute("""
            INSERT INTO alarm_state(alarm_id,stream_id,sequence,active,payload) VALUES(?,?,?,?,?)
            ON CONFLICT(alarm_id) DO UPDATE SET
                stream_id=excluded.stream_id, sequence=excluded.sequence,
                active=excluded.active, payload=excluded.payload
            WHERE alarm_state.stream_id != excluded.stream_id OR alarm_state.sequence < excluded.sequence
        """, (item["alarm_id"], stream_id, item["sequence"], int(item["active"]), json.dumps(item, ensure_ascii=False)))

    def _snapshot(self) -> None:
        cursor = None
        first = None
        items: list[dict] = []
        seen_cursors: set[str] = set()
        seen_alarms: set[str] = set()
        while True:
            page = self._fetch("snapshot", cursor)
            if first is None:
                first = page
            if any(page[name] != first[name] for name in ("device_id", "stream_id", "watermark", "events_cursor")):
                raise PollError("schema")
            for item in page["items"]:
                if item["alarm_id"] in seen_alarms:
                    raise PollError("schema")
                seen_alarms.add(item["alarm_id"])
                items.append(item)
            if not page["has_more"]:
                break
            cursor = page["next_cursor"]
            if cursor in seen_cursors:
                raise PollError("schema")
            seen_cursors.add(cursor)
        with self.db:
            old_stream = self._get("stream_id")
            if old_stream is not None and old_stream != first["stream_id"]:
                self.db.execute("DELETE FROM alarm_state")
            # A completed snapshot can clear only state at/before its watermark.
            self.db.execute(
                "UPDATE alarm_state SET active=0 WHERE stream_id=? AND sequence<=?",
                (first["stream_id"], first["watermark"]),
            )
            for item in items:
                self._apply_state(item, first["stream_id"])
                # Equal sequence rows were just deactivated above; restore them.
                self.db.execute(
                    "UPDATE alarm_state SET active=1 WHERE alarm_id=? AND stream_id=? AND sequence=?",
                    (item["alarm_id"], first["stream_id"], item["sequence"]),
                )
            self._put("device_id", first["device_id"])
            self._put("stream_id", first["stream_id"])
            self._put("snapshot_watermark", str(first["watermark"]))
            self._put("initialized", "1")
            self._put("resync_required", "0")
            # Replay retained history from its beginning, not events_cursor: a
            # fault and recovery that predate the snapshot still matter.
            self.db.execute("DELETE FROM metadata WHERE key IN ('cursor','event_sequence')")

    def _commit_events(self, page: dict) -> None:
        prior_seq = int(self._get("event_sequence") or "0")
        if page["watermark"] < prior_seq or (page["items"] and page["items"][0]["sequence"] <= prior_seq):
            raise PollError("schema")
        with self.db:
            snapshot_watermark = int(self._get("snapshot_watermark") or "0")
            for item in page["items"]:
                existing = self.db.execute(
                    "SELECT payload FROM events WHERE device_id=? AND stream_id=? AND (event_id=? OR sequence=?)",
                    (page["device_id"], page["stream_id"], item["event_id"], item["sequence"]),
                ).fetchall()
                if any(json.loads(row[0]) != item for row in existing):
                    raise PollError("schema")
                self.db.execute(
                    "INSERT OR IGNORE INTO events(device_id,stream_id,event_id,sequence,payload) VALUES(?,?,?,?,?)",
                    (page["device_id"], page["stream_id"], item["event_id"], item["sequence"], json.dumps(item, ensure_ascii=False)),
                )
                # Snapshot is authoritative for all state <= its watermark.
                # Replaying an old occurrence must not revive a recovered alarm
                # whose recovery has already aged out of retained history.
                if item["sequence"] > snapshot_watermark:
                    self._apply_state(item, page["stream_id"])
            self._put("cursor", page["next_cursor"])
            if page["items"]:
                self._put("event_sequence", str(page["items"][-1]["sequence"]))
            self._put("last_sync_at", str(self.clock()))

    def poll_once(self) -> bool:
        if not self._poll_lock.acquire(blocking=False):
            raise RuntimeError("one non-overlapping poll loop is required per device")
        try:
            if self._get("initialized") != "1" or self._get("resync_required") == "1":
                self._snapshot()
            resyncs = 0
            while True:
                cursor = self._get("cursor")
                try:
                    page = self._fetch("events", cursor)
                    if page["stream_id"] != self._get("stream_id"):
                        raise PollError("resync", 410, {"resync_reason": "stream_changed", **page})
                    if (cursor is None and page["retention_start_sequence"] >
                            int(self._get("snapshot_watermark") or "0") + 1):
                        # Snapshot commit and historical replay are separate
                        # transactions. A crash/offline interval between them
                        # must not silently skip a since-expired recovery.
                        raise PollError("resync", 410, {
                            "resync_reason": "retention_expired_after_snapshot",
                            "stream_id": page["stream_id"],
                            "retention_start_sequence": page["retention_start_sequence"],
                        })
                except PollError as exc:
                    if exc.status != 410 or resyncs >= 1:
                        raise
                    self._require_resync(exc.detail)
                    self._snapshot()
                    resyncs += 1
                    continue
                if page["items"] and page["next_cursor"] == cursor:
                    raise PollError("schema")
                self._commit_events(page)
                if not page["has_more"]:
                    break
            with self.db:
                self._put("last_error_kind", "")
                self._put("last_error_status", "")
            return True
        except (PollError, OSError, ValueError) as exc:
            if isinstance(exc, PollError):
                kind, status = exc.kind, exc.status
                if status == 410:
                    self._require_resync(exc.detail, history_gap=False)
            else:
                kind, status = "network", None
            with self.db:
                self._put("last_error_kind", kind)
                self._put("last_error_status", str(status or ""))
            logger.warning("alarm polling failed: kind=%s status=%s", kind, status)
            return False
        finally:
            self._poll_lock.release()

    def health(self) -> dict:
        last_valid = self._get("last_valid_response_at")
        reference = float(last_valid or self._get("monitoring_started_at") or self.clock())
        age = max(0.0, self.clock() - reference)
        return {
            "communication_alarm": age >= self.unavailable_after,
            "communication_code": "ALARM_SERVICE_UNAVAILABLE" if age >= self.unavailable_after else None,
            "origin": "business_backend",
            "seconds_since_valid_response": age,
            "last_error_kind": self._get("last_error_kind") or None,
            "last_error_status": self._get("last_error_status") or None,
            "last_sync_at": self._get("last_sync_at"),
            "resync_required": self._get("resync_required") == "1",
            "monitoring": json.loads(self._get("monitoring") or "{}"),
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="device HTTPS base URL")
    parser.add_argument("--database", default="backend-alarms.sqlite3")
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--allow-http", action="store_true", help="controlled local testing only")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.interval <= 0:
        parser.error("--interval must be positive")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        poller = AlarmPoller(args.url, args.database, os.getenv("COLONY_ALARM_TOKEN", ""), allow_http=args.allow_http)
    except ValueError as exc:
        parser.error(str(exc))
    try:
        while True:
            started = time.monotonic()
            succeeded = poller.poll_once()
            logger.info("alarm synchronization: success=%s health=%s", succeeded, json.dumps(poller.health(), ensure_ascii=False))
            if args.once:
                if not succeeded:
                    raise SystemExit(1)
                break
            time.sleep(max(0.0, args.interval - (time.monotonic() - started)))
    except KeyboardInterrupt:
        pass
    finally:
        poller.close()


if __name__ == "__main__":
    main()
