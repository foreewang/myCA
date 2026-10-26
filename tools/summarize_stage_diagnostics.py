"""Read-only report for single/multi-well result.json and scan_result.json."""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path


def summarize(result):
    if "wells" in result:
        scans = [w["result"]["capture_result"] for w in result["wells"] if w.get("result", {}).get("capture_result")]
    elif result.get("capture_result"):
        scans = [result["capture_result"]]
    elif "point_timings" in result:
        scans = [result]
    else:
        scans = []
    points = [p for scan in scans for p in scan.get("point_timings", [])]
    measured = [p["communication"] for p in points if "communication" in p]
    guard_keys = ("switch_guard_count", "switch_guard_wait_ms", "switch_guard_requested_ms")
    guard_measured = [c for c in measured if all(key in c for key in guard_keys)]
    steps = defaultdict(float)
    lifecycle = defaultdict(float)
    phases = {}
    for p in points:
        for name, value in p.get("motion_timings_ms", {}).items():
            steps[name] += value
    for comm in measured:
        for name, value in comm.get("lifecycle_ms", {}).items():
            lifecycle[name] += value
        for group in comm.get("groups", {}).values():
            row = phases.setdefault(group["phase"], {"transaction_count": 0, "total_ms": 0.0, "retry_count": 0})
            for key in row:
                row[key] += group[key]
    move_ms = sum(p.get("timings_ms", {}).get("move_total", 0) for p in points)
    other_points = [p for scan in scans for p in scan.get("point_timings", [])[1:]]
    return {
        "task_id": result.get("task_id"), "status": result.get("status"),
        "task_work_ms": result.get("timings_ms", {}).get("task_work"),
        "point_count": len(points), "captured_image_count": sum(s.get("image_count", 0) for s in scans),
        "move_total_ms": move_ms, "motion_steps_ms": dict(steps),
        "stop_shoot_options": [s.get("stop_shoot") for s in scans],
        "slave_switch_gap_ms": [s.get("slave_switch_gap_ms") for s in scans],
        "session_cleanup": [s.get("motion_session_cleanup", {}) for s in scans],
        "lifecycle_counts": {key: sum(c.get("lifecycle_counts", {}).get(key, 0) for c in measured) for key in ("connect", "disconnect")} if measured else None,
        "nonfirst_move_mean_ms": sum(p["timings_ms"]["move_total"] for p in other_points) / len(other_points) if other_points else None,
        "communication_measured_points": len(measured),
        "communication_complete": bool(points) and len(points) == len(measured),
        "switch_guard_measured_points": len(guard_measured),
        "switch_guard_complete": bool(points) and len(points) == len(guard_measured),
        "communication": {**{key: sum(c[key] for c in measured) if measured else None for key in (
            "transaction_count", "attempt_count", "retry_count", "failed_transaction_count",
            "transaction_total_ms", "attempt_total_ms", "retry_overhead_ms", "slow_transaction_count", "dropped_details",
        )}, **{key: sum(c[key] for c in guard_measured) if guard_measured else None for key in guard_keys}},
        "phase_communication": phases, "lifecycle_ms": dict(lifecycle),
        "move_unattributed_ms": move_ms - sum(steps.values()) - sum(lifecycle.values()),
        "note": "Communication and switch guards are INCLUDED in motion steps; never add these to move_total. Transaction/retry totals EXCLUDE the separately reported switch guard waits. Missing instrumentation is unknown, not zero retries/waits. Residual includes uninstrumented work and diagnostics overhead.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="+", type=Path)
    args = parser.parse_args()
    for path in args.files:
        result = json.loads(path.read_text(encoding="utf-8-sig"))
        print(json.dumps({"file": str(path), **summarize(result)}, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
