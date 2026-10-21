"""Read-only summary of result.json/scan_result.json files for A/B trials."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def summarize(data: dict) -> dict:
    wells = [w["result"] for w in data["wells"]] if "wells" in data else [data]
    points = []
    captures = []
    later_points = []
    for well in wells:
        scan = well.get("capture_result") or well
        ps = scan.get("point_timings", [])
        points.extend(ps)
        later_points.extend(ps[1:])
        captures.extend(scan.get("captures", []))
    reports = [p["communication"] for p in points if "communication" in p]
    errors = [abs(value) for c in captures
              for value in (c.get("motion_result", {}).get("err_to_target") or {}).values()
              if isinstance(value, (int, float))]
    result = {
        "task_id": data.get("task_id"), "status": data.get("status"),
        "stages": data.get("stages"), "well_count": len(wells),
        "point_count": len(points), "captured_image_count": len(captures),
        "task_work_ms": data.get("timings_ms", {}).get("task_work"),
        "move_total_ms": sum(p["timings_ms"].get("move_total", 0) for p in points),
        "nonfirst_move_mean_ms": (
            sum(p["timings_ms"].get("move_total", 0) for p in later_points) / len(later_points)
            if later_points else None),
        "max_arrival_error_pulse": max(errors) if errors else None,
        "communication_measured_points": len(reports),
        "communication_complete": bool(points) and len(reports) == len(points),
        "slave_switch_gap_s": sorted({r["slave_switch_gap_s"] for r in reports}),
    }
    for name in ("transaction_count", "attempt_count", "retry_count", "failed_transaction_count",
                 "slave_switch_count", "gap_wait_count", "gap_wait_ms",
                 "transaction_total_ms", "retry_overhead_ms", "dropped_details"):
        # Missing measurements are unknown, not a measured zero.
        result[name] = sum(r[name] for r in reports) if reports else None
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path, nargs="+")
    args = parser.parse_args()
    for path in args.results:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        print(json.dumps({"file": str(path), **summarize(data)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
