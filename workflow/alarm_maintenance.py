"""Offline maintenance after restoring an alarm database from backup."""
from __future__ import annotations

import argparse
import json
import secrets
import sqlite3
from pathlib import Path
from uuid import uuid4

from workflow.path_guard import PROJECT_ROOT
from workflow.process_guard import SingleInstanceLock


def rotate_stream(database: Path) -> str:
    """Caller must hold the same exclusive instance lock as the API service."""
    stream_id = uuid4().hex
    with sqlite3.connect(database.resolve().as_uri() + "?mode=rw", uri=True, timeout=1) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE meta SET value=? WHERE key='stream_id'", (stream_id,))
        db.execute("UPDATE meta SET value=? WHERE key='secret'", (secrets.token_hex(32),))
        for sequence, payload in db.execute("SELECT sequence,payload FROM events").fetchall():
            item = json.loads(payload)
            item["event_id"] = f"{stream_id}:{sequence}"
            db.execute("UPDATE events SET payload=? WHERE sequence=?", (json.dumps(item, ensure_ascii=False), sequence))
        for fingerprint, payload in db.execute("SELECT fingerprint,payload FROM alarms").fetchall():
            item = json.loads(payload)
            item["event_id"] = f"{stream_id}:{item['sequence']}"
            db.execute("UPDATE alarms SET payload=? WHERE fingerprint=?", (json.dumps(item, ensure_ascii=False), fingerprint))
    return stream_id


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["rotate-stream"])
    parser.add_argument("--database", type=Path, required=True)
    args = parser.parse_args()
    lock = SingleInstanceLock(PROJECT_ROOT / "data" / "api_server.lock", owner="alarm-maintenance")
    lock.acquire()
    try:
        print(json.dumps({"stream_id": rotate_stream(args.database)}))
    finally:
        lock.release()


if __name__ == "__main__":
    main()
