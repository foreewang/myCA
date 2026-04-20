#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
比较两次 A1 扫描生成的 scan_manifest.json，评估位移台重复定位误差。

比较逻辑：
- 以 capture.index 为主键对齐两次扫描中的同一拍照点。
- 使用 capture.after.x.current_pos / capture.after.y.current_pos 作为该点的实际到位坐标。
- 计算 run2 - run1 的重复定位差值（脉冲、mm、um）。
- 另外统计每一轮各点相对目标坐标的跟踪误差。

输出：
- compare_points.csv    每个点的详细误差表
- compare_summary.json  汇总统计

示例：
python compare_scan_manifests.py \
  --run1 C:\colony_system\data\images\A1_scan\scan_manifest.json \
  --run2 C:\colony_system\data\images\A1_scan_check\scan_manifest.json \
  --out-dir C:\colony_system\data\images\A1_compare
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
from statistics import mean
from typing import Any, Dict, List, Tuple


def load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def get_nested(d: Dict[str, Any], *keys: str, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def index_captures_by_id(manifest: Dict[str, Any]) -> Dict[int, Dict[str, Any]]:
    captures = manifest.get("captures", [])
    out: Dict[int, Dict[str, Any]] = {}
    for cap in captures:
        idx = int(cap["index"])
        out[idx] = cap
    return out


def pulse_to_mm(pulse: float, pulses_per_mm: float) -> float:
    return pulse / pulses_per_mm


def safe_norm(x: float, y: float) -> float:
    return math.hypot(x, y)


def summarize(values: List[float]) -> Dict[str, float | None]:
    if not values:
        return {"count": 0, "mean": None, "max": None, "min": None, "rms": None}
    rms = math.sqrt(sum(v * v for v in values) / len(values))
    return {
        "count": len(values),
        "mean": mean(values),
        "max": max(values),
        "min": min(values),
        "rms": rms,
    }


def build_row(idx: int, cap1: Dict[str, Any], cap2: Dict[str, Any], pulses_per_mm: float) -> Dict[str, Any]:
    target_x = float(cap1["stage_x_target"])
    target_y = float(cap1["stage_y_target"])

    x1 = float(get_nested(cap1, "after", "x", "current_pos"))
    y1 = float(get_nested(cap1, "after", "y", "current_pos"))
    x2 = float(get_nested(cap2, "after", "x", "current_pos"))
    y2 = float(get_nested(cap2, "after", "y", "current_pos"))

    dx_repeat_pulse = x2 - x1
    dy_repeat_pulse = y2 - y1
    norm_repeat_pulse = safe_norm(dx_repeat_pulse, dy_repeat_pulse)

    dx_repeat_mm = pulse_to_mm(dx_repeat_pulse, pulses_per_mm)
    dy_repeat_mm = pulse_to_mm(dy_repeat_pulse, pulses_per_mm)
    norm_repeat_mm = pulse_to_mm(norm_repeat_pulse, pulses_per_mm)

    ex1_pulse = x1 - target_x
    ey1_pulse = y1 - target_y
    en1_pulse = safe_norm(ex1_pulse, ey1_pulse)
    ex2_pulse = x2 - target_x
    ey2_pulse = y2 - target_y
    en2_pulse = safe_norm(ex2_pulse, ey2_pulse)

    ex1_mm = pulse_to_mm(ex1_pulse, pulses_per_mm)
    ey1_mm = pulse_to_mm(ey1_pulse, pulses_per_mm)
    en1_mm = pulse_to_mm(en1_pulse, pulses_per_mm)
    ex2_mm = pulse_to_mm(ex2_pulse, pulses_per_mm)
    ey2_mm = pulse_to_mm(ey2_pulse, pulses_per_mm)
    en2_mm = pulse_to_mm(en2_pulse, pulses_per_mm)

    row_index = cap1.get("row_index")
    col_index = cap1.get("col_index")
    view_down_mm = cap1.get("view_down_mm")
    view_right_mm = cap1.get("view_right_mm")

    return {
        "index": idx,
        "row_index": row_index,
        "col_index": col_index,
        "view_down_mm": view_down_mm,
        "view_right_mm": view_right_mm,
        "target_x": target_x,
        "target_y": target_y,
        "run1_after_x": x1,
        "run1_after_y": y1,
        "run2_after_x": x2,
        "run2_after_y": y2,
        "repeat_dx_pulse": dx_repeat_pulse,
        "repeat_dy_pulse": dy_repeat_pulse,
        "repeat_norm_pulse": norm_repeat_pulse,
        "repeat_dx_mm": dx_repeat_mm,
        "repeat_dy_mm": dy_repeat_mm,
        "repeat_norm_mm": norm_repeat_mm,
        "repeat_dx_um": dx_repeat_mm * 1000.0,
        "repeat_dy_um": dy_repeat_mm * 1000.0,
        "repeat_norm_um": norm_repeat_mm * 1000.0,
        "run1_track_err_x_pulse": ex1_pulse,
        "run1_track_err_y_pulse": ey1_pulse,
        "run1_track_err_norm_pulse": en1_pulse,
        "run1_track_err_x_mm": ex1_mm,
        "run1_track_err_y_mm": ey1_mm,
        "run1_track_err_norm_mm": en1_mm,
        "run2_track_err_x_pulse": ex2_pulse,
        "run2_track_err_y_pulse": ey2_pulse,
        "run2_track_err_norm_pulse": en2_pulse,
        "run2_track_err_x_mm": ex2_mm,
        "run2_track_err_y_mm": ey2_mm,
        "run2_track_err_norm_mm": en2_mm,
    }


def write_csv(rows: List[Dict[str, Any]], path: Path) -> None:
    if not rows:
        raise ValueError("No rows to write")
    fieldnames = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def compare(run1_path: Path, run2_path: Path, out_dir: Path) -> Tuple[Path, Path]:
    m1 = load_json(run1_path)
    m2 = load_json(run2_path)

    ppm1 = float(get_nested(m1, "reference", "pulses_per_mm"))
    ppm2 = float(get_nested(m2, "reference", "pulses_per_mm"))
    if abs(ppm1 - ppm2) > 1e-9:
        raise ValueError(f"pulses_per_mm mismatch: {ppm1} vs {ppm2}")
    pulses_per_mm = ppm1

    caps1 = index_captures_by_id(m1)
    caps2 = index_captures_by_id(m2)
    common_ids = sorted(set(caps1) & set(caps2))
    missing_in_run2 = sorted(set(caps1) - set(caps2))
    missing_in_run1 = sorted(set(caps2) - set(caps1))

    if not common_ids:
        raise ValueError("No common capture indices found between the two manifests")

    rows: List[Dict[str, Any]] = []
    for idx in common_ids:
        rows.append(build_row(idx, caps1[idx], caps2[idx], pulses_per_mm))

    repeat_dx = [abs(float(r["repeat_dx_pulse"])) for r in rows]
    repeat_dy = [abs(float(r["repeat_dy_pulse"])) for r in rows]
    repeat_norm = [float(r["repeat_norm_pulse"]) for r in rows]
    repeat_norm_mm = [float(r["repeat_norm_mm"]) for r in rows]

    summary = {
        "run1": str(run1_path),
        "run2": str(run2_path),
        "plate_type": m1.get("plate_type"),
        "well": m1.get("well"),
        "pulses_per_mm": pulses_per_mm,
        "scan_config_run1": m1.get("scan_config", {}),
        "scan_config_run2": m2.get("scan_config", {}),
        "common_point_count": len(common_ids),
        "missing_in_run2": missing_in_run2,
        "missing_in_run1": missing_in_run1,
        "repeatability_pulse": {
            "abs_dx": summarize(repeat_dx),
            "abs_dy": summarize(repeat_dy),
            "norm": summarize(repeat_norm),
        },
        "repeatability_mm": {
            "norm": summarize(repeat_norm_mm),
        },
        "worst_points_by_norm_pulse": [
            {
                "index": int(r["index"]),
                "row_index": int(r["row_index"]),
                "col_index": int(r["col_index"]),
                "repeat_dx_pulse": r["repeat_dx_pulse"],
                "repeat_dy_pulse": r["repeat_dy_pulse"],
                "repeat_norm_pulse": r["repeat_norm_pulse"],
                "repeat_norm_um": r["repeat_norm_um"],
            }
            for r in sorted(rows, key=lambda x: float(x["repeat_norm_pulse"]), reverse=True)[:10]
        ],
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "compare_points.csv"
    json_path = out_dir / "compare_summary.json"
    write_csv(rows, csv_path)
    with json_path.open("w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    return csv_path, json_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Compare two A1 scan_manifest.json files")
    parser.add_argument("--run1", required=True, help="第一次扫描的 scan_manifest.json")
    parser.add_argument("--run2", required=True, help="第二次扫描的 scan_manifest.json")
    parser.add_argument("--out-dir", required=True, help="输出目录")
    args = parser.parse_args()

    csv_path, json_path = compare(Path(args.run1), Path(args.run2), Path(args.out_dir))
    print(f"Saved point-wise comparison to: {csv_path}")
    print(f"Saved summary to: {json_path}")
