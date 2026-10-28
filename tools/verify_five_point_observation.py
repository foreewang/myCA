"""Offline acceptance of every configured 4x five-point target; never opens hardware."""
from __future__ import annotations

import argparse
import itertools
import json
import math
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from workflow.config_validator import validate_objectives_file, validate_plates_file
from workflow.plate_geometry import all_well_names, compute_well_start, get_axis_pulses_per_mm
from workflow.scan_planner import plan_single_well_scan

POINT_NAMES = ["center", "upper_left", "upper_right", "lower_left", "lower_right"]
QUADRANTS = [(0, 0), (-1, -1), (1, -1), (-1, 1), (1, 1)]


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def verify(plates_path: Path, objectives_path: Path) -> dict:
    plates = validate_plates_file(plates_path)["plates"]
    objectives = validate_objectives_file(objectives_path)["objectives"]
    fov = objectives["4x"]["fov_mm"]
    width, height = float(fov["width"]), float(fov["height"])
    require(math.isfinite(width) and math.isfinite(height) and min(width, height) > 0,
            "4x FOV must be finite and positive")
    report = {
        "software_geometry_status": "passed",
        "hardware_acceptance": "not_run",
        "config_paths": {"plates": str(plates_path.resolve()), "objectives": str(objectives_path.resolve())},
        "plate_count": len(plates), "well_count": 0, "point_count": 0,
        "plate_summaries": [], "well_plans": [],
    }
    for plate_type, plate in plates.items():
        radius = float(plate["well_diameter_mm"]) / 2
        reference_half_width = float(plate["a1_start"]["reference_fov_mm"]["width"]) / 2
        margin = float(plate["five_point"]["edge_margin_mm"])
        px, py = get_axis_pulses_per_mm(plate)
        sx, sy = plate["x_stage_sign_for_view_right"], plate["y_stage_sign_for_view_down"]
        limits = plate["stage_limits"]
        safe = {"x": (limits["x_min"] + limits["safety_margin"], limits["x_max"] - limits["safety_margin"]),
                "y": (limits["y_min"] + limits["safety_margin"], limits["y_max"] - limits["safety_margin"])}
        well_names = all_well_names(plate)
        minimum_clearance = math.inf
        overlaps = set()
        offset_x_values, offset_y_values = [], []
        for well_name in well_names:
            label = f"{plate_type}/{well_name}"
            params = {"task_id": "five-point-offline-acceptance", "task_type": "capture",
                      "plate_type": plate_type, "well_name": well_name,
                      "objective_name": "4x", "fov_mm": fov}
            plan = plan_single_well_scan({"plate": plate}, params)
            points, scan = plan["points"], plan["scan_config"]
            require(scan["mode"] == "five_point", f"{label}: 4x default is not five_point")
            require(len(points) == 5 and scan["point_count"] == 5 and scan["clipped_point_count"] == 0,
                    f"{label}: five complete points are required")
            require([p["point_name"] for p in points] == POINT_NAMES, f"{label}: unexpected point order")
            require([p["index"] for p in points] == list(range(1, 6)), f"{label}: unexpected point indices")
            require(len({(p["stage_x_target"], p["stage_y_target"]) for p in points}) == 5,
                    f"{label}: duplicate target")
            start = compute_well_start(plate, well_name)
            center = (start["x"] + sx * (radius - reference_half_width) * px, start["y"])
            offsets = []
            for point, (sign_x, sign_y) in zip(points, QUADRANTS):
                x, y = point["stage_x_target"], point["stage_y_target"]
                dx, dy = (x - center[0]) / (sx * px), (y - center[1]) / (sy * py)
                offsets.append((dx, dy))
                require(safe["x"][0] <= x <= safe["x"][1] and safe["y"][0] <= y <= safe["y"][1],
                        f"{label}/{point['point_name']}: stage limit violation")
                if sign_x == 0:
                    require(abs(x - center[0]) <= 0.500001 and abs(y - center[1]) <= 0.500001,
                            f"{label}: first point is not the physical center")
                else:
                    require(sign_x * dx > 0 and sign_y * dy > 0, f"{label}: wrong quadrant")
                    offset_x_values.append(abs(dx))
                    offset_y_values.append(abs(dy))
                # Independently inspect all four corners of the quantized field.
                for cx, cy in itertools.product((-width / 2, width / 2), (-height / 2, height / 2)):
                    clearance = radius - math.hypot(dx + cx, dy + cy)
                    minimum_clearance = min(minimum_clearance, clearance)
                    require(clearance >= margin - 1e-10,
                            f"{label}/{point['point_name']}: corner clearance {clearance} < {margin}")
            actual_overlap = any(abs(a[0] - b[0]) < width and abs(a[1] - b[1]) < height
                                 for a, b in itertools.combinations(offsets, 2))
            require(scan["has_overlapping_views"] is actual_overlap, f"{label}: incorrect overlap metadata")
            overlaps.add(actual_overlap)
            report["well_plans"].append(plan)
            report["well_count"] += 1
            report["point_count"] += len(points)

        # Verify the public mode contract without opening any device.
        ten_params = {**params, "objective_name": "10x", "fov_mm": objectives["10x"]["fov_mm"]}
        ten = plan_single_well_scan({"plate": plate}, ten_params)
        require(ten["scan_config"]["mode"] == "full_coverage", f"{plate_type}: 10x default changed")
        try:
            plan_single_well_scan({"plate": plate}, {**ten_params, "scan_mode": "five_point"})
        except ValueError:
            pass
        else:
            raise ValueError(f"{plate_type}: 10x five_point was not rejected")
        full = plan_single_well_scan({"plate": plate}, {**params, "scan_mode": "full_coverage"})
        require(full["scan_config"]["mode"] == "full_coverage", f"{plate_type}: explicit full_coverage failed")
        report["plate_summaries"].append({
            "plate_type": plate_type, "well_count": len(well_names), "point_count": len(well_names) * 5,
            "minimum_corner_clearance_mm": minimum_clearance,
            "required_corner_clearance_mm": margin,
            "axis_offset_x_range_mm": [min(offset_x_values), max(offset_x_values)],
            "axis_offset_y_range_mm": [min(offset_y_values), max(offset_y_values)],
            "has_overlapping_views": sorted(overlaps),
            "mode_contract": "4x=five_point; 10x=full_coverage; explicit 10x five_point rejected",
        })
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plates", type=Path, default=PROJECT_ROOT / "config/plates.yaml")
    parser.add_argument("--objectives", type=Path, default=PROJECT_ROOT / "config/objectives.yaml")
    parser.add_argument("--dump-json", type=Path, help="Write full plans and acceptance evidence to this file")
    args = parser.parse_args()
    try:
        report = verify(args.plates, args.objectives)
    except (ValueError, KeyError, TypeError) as exc:
        parser.exit(1, f"Five-point offline acceptance failed: {exc}\n")
    if args.dump_json:
        args.dump_json.parent.mkdir(parents=True, exist_ok=True)
        args.dump_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "well_plans"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
