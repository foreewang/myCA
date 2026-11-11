"""Durable alarm state and ordered event history; no hardware dependencies."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4


def utc_timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


class AlarmQueryError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.status = status
        self.detail = {"error_code": code, "message": message, **extra}


class AlarmStore:
    """One SQLite transaction changes state and appends its event.

    A separate connection per operation avoids sharing cursors across threads.
    Snapshot pages use a bounded process-local cache of *frozen* records, never
    a live table OFFSET. A process restart explicitly expires these cursors.
    """

    def __init__(self, path: Path, *, device_id: str | None = None,
                 clock: Callable[[], float] = time.time, snapshot_ttl: float = 600,
                 retention_days: float = 30):
        self.path = Path(path)
        self.clock = clock
        self.snapshot_ttl = snapshot_ttl
        self.retention_days = retention_days
        self.boot_id = uuid4().hex
        self._snapshots: dict[str, dict[str, Any]] = {}
        self._snapshot_lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path, timeout=0.25) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS alarms (
                    fingerprint TEXT PRIMARY KEY, active INTEGER NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    recorded_at REAL NOT NULL, payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS events_time ON events(recorded_at);
                CREATE TABLE IF NOT EXISTS facts (fingerprint TEXT PRIMARY KEY, alarm_id TEXT NOT NULL);
            """)
            for key, value in {"schema_version": "1", "stream_id": uuid4().hex,
                               "device_id": device_id or uuid4().hex,
                               "secret": secrets.token_hex(32), "retention_floor": "0",
                               "last_sequence": "0"}.items():
                db.execute("INSERT OR IGNORE INTO meta VALUES (?,?)", (key, value))
            meta = dict(db.execute("SELECT key,value FROM meta"))
            if meta["schema_version"] != "1":
                raise RuntimeError("unsupported alarm database schema")
            if device_id and device_id != meta["device_id"]:
                raise RuntimeError("alarm device identity differs from stored identity")
        self.device_id = meta["device_id"]
        self.stream_id = meta["stream_id"]
        self._secret = bytes.fromhex(meta["secret"])

    @contextmanager
    def _connection(self, *, write: bool = False):
        # mode=rw is deliberate: a missing/replaced database is never silently
        # re-created by a query or a hook in the running process.
        db = sqlite3.connect(self.path.resolve().as_uri() + "?mode=rw", uri=True, timeout=0.25)
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            identity = dict(db.execute("SELECT key,value FROM meta"))
            if identity.get("stream_id") != self.stream_id:
                raise RuntimeError("alarm database replaced; restart alarm service")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def probe(self) -> None:
        with self._connection(write=True) as db:
            # Real write probe: reading alone does not prove disk/write health.
            db.execute("INSERT OR REPLACE INTO meta VALUES ('last_write_probe',?)", (str(self.clock()),))

    def active_keys(self) -> set[tuple[str, str, str]]:
        with self._connection() as db:
            items = (json.loads(row[0]) for row in db.execute("SELECT payload FROM alarms WHERE active=1"))
            return {(item["code"], item["component"], item["resource"]) for item in items}

    @staticmethod
    def _fingerprint(code: str, component: str, resource: str, fact_key: str | None) -> str:
        return hashlib.sha256(json.dumps([code, component, resource, fact_key],
                                         ensure_ascii=False).encode()).hexdigest()

    def record(self, *, code: str, category: str, severity: str, message: str,
               component: str, resource: str = "default", task_id: str | None = None,
               execution_id: str | None = None, diagnostic_id: str | None = None,
               fact: bool = False, occurrence_key: str | None = None,
               recovery: bool = False, occurred_at: str | None = None) -> str | None:
        fingerprint = self._fingerprint(code, component, resource,
                                        (occurrence_key or uuid4().hex) if fact else None)
        now = self.clock()
        event_time = utc_timestamp(now)
        if occurred_at:
            try:
                parsed = datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
                if parsed.tzinfo is not None:
                    event_time = parsed.astimezone(timezone.utc).isoformat()
            except (ValueError, TypeError, AttributeError):
                pass
        with self._connection(write=True) as db:
            if fact:
                previous = db.execute("SELECT alarm_id FROM facts WHERE fingerprint=?", (fingerprint,)).fetchone()
                if previous:
                    return previous[0]
            row = db.execute("SELECT payload FROM alarms WHERE fingerprint=?", (fingerprint,)).fetchone()
            previous = json.loads(row[0]) if row else None
            if recovery and (not previous or not previous["active"]):
                return None
            continuing = previous is not None and previous["active"] and not fact
            alarm_id = previous["alarm_id"] if continuing else uuid4().hex
            item = {
                "alarm_id": alarm_id, "category": category, "code": code,
                "severity": severity, "message": message, "component": component,
                "resource": resource, "task_id": task_id, "execution_id": execution_id,
                "diagnostic_id": diagnostic_id, "active": not (fact or recovery),
                "transition": "recorded" if fact else "recovered" if recovery else "updated" if continuing else "occurred",
                "count": previous["count"] + (0 if recovery else 1) if continuing else 1,
                "first_occurred_at": previous["first_occurred_at"] if continuing else event_time,
                "occurred_at": event_time, "recorded_at": utc_timestamp(now),
            }
            if recovery:
                # Preserve original incident correlation and severity.
                for key in ("severity", "task_id", "execution_id", "diagnostic_id"):
                    item[key] = previous[key]
            seq = db.execute("INSERT INTO events(recorded_at,payload) VALUES (?,?)", (now, "{}")).lastrowid
            item.update(sequence=seq, event_id=f"{self.stream_id}:{seq}")
            payload = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
            db.execute("UPDATE events SET payload=? WHERE sequence=?", (payload, seq))
            db.execute("UPDATE meta SET value=? WHERE key='last_sequence'", (str(seq),))
            if fact:
                db.execute("INSERT INTO facts VALUES (?,?)", (fingerprint, alarm_id))
            else:
                db.execute("INSERT OR REPLACE INTO alarms VALUES (?,?,?)", (fingerprint, int(item["active"]), payload))
            return alarm_id

    def prune(self) -> None:
        cutoff = self.clock() - self.retention_days * 86400
        with self._connection(write=True) as db:
            # Delete a contiguous prefix only, even if wall time moved back.
            first_live = db.execute("SELECT MIN(sequence) FROM events WHERE recorded_at>=?", (cutoff,)).fetchone()[0]
            last = int(db.execute("SELECT value FROM meta WHERE key='last_sequence'").fetchone()[0])
            floor = first_live - 1 if first_live is not None else last
            old_floor = int(db.execute("SELECT value FROM meta WHERE key='retention_floor'").fetchone()[0])
            floor = max(floor, old_floor)
            db.execute("DELETE FROM events WHERE sequence<=?", (floor,))
            db.execute("UPDATE meta SET value=? WHERE key='retention_floor'", (str(floor),))
        # Active states and fact deduplication survive event retention.

    def _encode(self, payload: dict[str, Any]) -> str:
        data = json.dumps({"v": 1, "stream": self.stream_id, **payload}, separators=(",", ":")).encode()
        token = base64.urlsafe_b64encode(data).rstrip(b"=")
        signature = hmac.new(self._secret, token, hashlib.sha256).hexdigest().encode()
        return (token + b"." + signature).decode()

    def _expired(self, reason: str, floor: int) -> AlarmQueryError:
        return AlarmQueryError(410, "ALARM_CURSOR_EXPIRED", "报警同步位置已失效，请重新同步",
                               resync_reason=reason, stream_id=self.stream_id,
                               retention_start_sequence=floor + 1)

    def _decode(self, cursor: str, mode: str, floor: int) -> dict[str, Any]:
        try:
            if len(cursor) > 4096:
                raise ValueError()
            token, sig = cursor.encode("ascii").split(b".")
            payload = json.loads(base64.urlsafe_b64decode(token + b"=" * (-len(token) % 4)))
            if not isinstance(payload, dict) or payload.get("v") != 1:
                raise ValueError()
            if payload.get("stream") != self.stream_id:
                raise self._expired("stream_changed", floor)
            if not hmac.compare_digest(sig, hmac.new(self._secret, token, hashlib.sha256).hexdigest().encode()):
                raise ValueError()
            if payload.get("mode") != mode:
                raise ValueError()
            return payload
        except AlarmQueryError:
            raise
        except (ValueError, TypeError, UnicodeError, KeyError):
            raise AlarmQueryError(400, "ALARM_CURSOR_INVALID", "报警同步位置不合法") from None

    def query(self, *, mode: str = "events", cursor: str | None = None, limit: int = 100) -> dict[str, Any]:
        if mode not in ("events", "snapshot") or not 1 <= limit <= 500:
            raise AlarmQueryError(400, "ALARM_QUERY_INVALID", "报警查询参数不合法")
        with self._connection() as db:
            meta = dict(db.execute("SELECT key,value FROM meta"))
            floor, watermark = int(meta["retention_floor"]), int(meta["last_sequence"])
            data = self._decode(cursor, mode, floor) if cursor else {}
            common = {"protocol_version": 1, "device_id": self.device_id, "stream_id": self.stream_id,
                      "boot_id": self.boot_id, "mode": mode, "retention_start_sequence": floor + 1,
                      "watermark": watermark}
            if mode == "events":
                after = data.get("after", floor)
                if type(after) is not int or after < 0:
                    raise AlarmQueryError(400, "ALARM_CURSOR_INVALID", "报警同步位置不合法")
                if after < floor or after > watermark:
                    raise self._expired("retention_expired" if after < floor else "sequence_rolled_back", floor)
                rows = db.execute("SELECT payload FROM events WHERE sequence>? AND sequence<=? ORDER BY sequence LIMIT ?",
                                  (after, watermark, limit + 1)).fetchall()
                items = [json.loads(row[0]) for row in rows[:limit]]
                more = len(rows) > limit
                next_after = items[-1]["sequence"] if more else watermark
                return {**common, "items": items, "has_more": more,
                        "next_cursor": self._encode({"mode": "events", "after": next_after})}
            with self._snapshot_lock:
                now = self.clock()
                self._snapshots = {key: value for key, value in self._snapshots.items() if value["expires"] > now}
                if cursor:
                    snapshot = self._snapshots.get(data.get("snapshot"))
                    offset = data.get("offset")
                    if snapshot is None:
                        raise self._expired("snapshot_expired", floor)
                    if type(offset) is not int or not 0 <= offset <= len(snapshot["items"]):
                        raise AlarmQueryError(400, "ALARM_CURSOR_INVALID", "报警同步位置不合法")
                    snapshot_id = data["snapshot"]
                else:
                    if len(self._snapshots) >= 64:
                        del self._snapshots[next(iter(self._snapshots))]
                    snapshot_id, offset = uuid4().hex, 0
                    snapshot = {"expires": now + self.snapshot_ttl, "watermark": watermark,
                                "items": [json.loads(row[0]) for row in db.execute(
                                    "SELECT payload FROM alarms WHERE active=1 ORDER BY fingerprint")]}
                    self._snapshots[snapshot_id] = snapshot
                items = snapshot["items"][offset:offset + limit]
                more = offset + limit < len(snapshot["items"])
                return {**common, "watermark": snapshot["watermark"], "items": items, "has_more": more,
                        "snapshot_expires_at": utc_timestamp(snapshot["expires"]),
                        "next_cursor": self._encode({"mode": "snapshot", "snapshot": snapshot_id,
                                                      "offset": offset + limit}) if more else None,
                        "events_cursor": self._encode({"mode": "events", "after": snapshot["watermark"]})}
