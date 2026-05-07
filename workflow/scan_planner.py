"""
为单个培养孔生成扫描路径点位

路径点全部生成后，先检查每个点是否在限位范围内。
"""
from __future__ import annotations

from math import sqrt
from typing import Dict, List, Any

from workflow.plate_geometry import (
    compute_well_start,
    get_a1_start,
    get_plate_pitch_mm,
    get_pulses_per_mm,
    get_view_signs,
    require_number,
)


def _row_values(step_y: float, radius: float) -> List[float]:
    """生成扫描时各行对应的“视野向下偏移量”列表。"""
    vals = [0.0]
    k = 1
    while True:
        y = round(k * step_y, 6)
        if y > radius:
            break
        vals.append(-y)
        vals.append(+y)
        k += 1
    return vals


def _x_positions_for_row(abs_vdown_mm: float, step_x: float, radius: float) -> List[float]:
    """计算某一扫描行内，所有有效的横向扫描位置。"""
    half_chord = sqrt(max(radius * radius - abs_vdown_mm * abs_vdown_mm, 0.0))
    x_left = radius - half_chord
    x_right = radius + half_chord

    xs = [round(x_left, 6)]
    x = x_left + step_x
    while x < x_right - 1e-6:
        xs.append(round(x, 6))
        x += step_x

    if abs(xs[-1] - x_right) > 1e-6:
        xs.append(round(x_right, 6))

    return xs


def _get_stage_limits(plate: Dict[str, Any]) -> Dict[str, Any]:
    cfg = plate.get("stage_limits", {}) or {}
    return {
        "enabled": bool(cfg.get("enabled", False)),
        "x_min": int(cfg.get("x_min", 0)) if cfg.get("x_min") is not None else None,
        "x_max": int(cfg.get("x_max", 0)) if cfg.get("x_max") is not None else None,
        "y_min": int(cfg.get("y_min", 0)) if cfg.get("y_min") is not None else None,
        "y_max": int(cfg.get("y_max", 0)) if cfg.get("y_max") is not None else None,
        "safety_margin": int(cfg.get("safety_margin", 0)),
    }


def _precheck_stage_limits(points: List[Dict[str, Any]], stage_limits: Dict[str, Any]) -> Dict[str, Any]:
    if not stage_limits["enabled"]:
        return {
            "enabled": False,
            "checked_point_count": len(points),
            "violations": [],
        }

    required = ["x_min", "x_max", "y_min", "y_max"]
    for k in required:
        if stage_limits[k] is None:
            raise ValueError(f"stage_limits.enabled=true，但缺少 {k}")

    x_lo = stage_limits["x_min"] + stage_limits["safety_margin"]
    x_hi = stage_limits["x_max"] - stage_limits["safety_margin"]
    y_lo = stage_limits["y_min"] + stage_limits["safety_margin"]
    y_hi = stage_limits["y_max"] - stage_limits["safety_margin"]

    violations: List[Dict[str, Any]] = []
    for p in points:
        x = int(p["stage_x_target"])
        y = int(p["stage_y_target"])

        reasons = []
        if x < x_lo:
            reasons.append(f"x<{x_lo}")
        if x > x_hi:
            reasons.append(f"x>{x_hi}")
        if y < y_lo:
            reasons.append(f"y<{y_lo}")
        if y > y_hi:
            reasons.append(f"y>{y_hi}")

        if reasons:
            violations.append(
                {
                    "index": int(p["index"]),
                    "row_index": int(p["row_index"]),
                    "col_index": int(p["col_index"]),
                    "stage_x_target": x,
                    "stage_y_target": y,
                    "reason": "; ".join(reasons),
                }
            )

    if violations:
        preview = violations[:5]
        raise ValueError(
            "扫描路径越出位移台安全范围，任务已在扫描前终止。"
            f" 共 {len(violations)} 个点越界，示例: {preview}"
        )

    return {
        "enabled": True,
        "checked_point_count": len(points),
        "safe_range": {
            "x_min_safe": x_lo,
            "x_max_safe": x_hi,
            "y_min_safe": y_lo,
            "y_max_safe": y_hi,
        },
        "violations": [],
    }


def plan_single_well_scan(ctx: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
    """为单个培养孔生成完整扫描计划。"""
    plate = ctx["plate"]
    well_name = params["well_name"]

    well_start = compute_well_start(plate, well_name)
    a1_start = get_a1_start(plate)
    ppm = get_pulses_per_mm(plate)
    x_sign, y_sign = get_view_signs(plate)

    well_diameter_mm = require_number(plate.get("well_diameter_mm"), "well_diameter_mm")
    well_gap_mm = require_number(plate.get("well_gap_mm"), "well_gap_mm")
    pitch_mm = get_plate_pitch_mm(plate)

    fov_w = require_number(params["fov_mm"]["width"], "fov_mm.width")
    fov_h = require_number(params["fov_mm"]["height"], "fov_mm.height")
    overlap = require_number(params.get("overlap"), "overlap")

    if fov_w <= 0 or fov_h <= 0:
        raise ValueError(f"视野尺寸必须大于 0，当前 fov_mm=({fov_w}, {fov_h})")
    if not (0.0 <= overlap < 1.0):
        raise ValueError(f"overlap 必须满足 0 <= overlap < 1，当前为 {overlap}")

    step_x = fov_w * (1.0 - overlap)
    step_y = fov_h * (1.0 - overlap)

    if step_x <= 0 or step_y <= 0:
        raise ValueError(f"扫描步长必须大于 0，当前 step_mm=({step_x}, {step_y})")

    radius = well_diameter_mm / 2.0
    row_vals = _row_values(step_y, radius)

    points = []
    idx = 1

    for row_index, vdown in enumerate(row_vals):
        xs = _x_positions_for_row(
            abs_vdown_mm=abs(vdown),
            step_x=step_x,
            radius=radius,
        )

        if row_index % 2 == 1:
            xs = list(reversed(xs))

        for col_index, vright in enumerate(xs):
            stage_x = int(round(well_start["x"] + x_sign * vdown * ppm))
            stage_y = int(round(well_start["y"] + y_sign * vright * ppm))

            points.append(
                {
                    "index": idx,
                    "row_index": row_index,
                    "col_index": col_index,
                    "view_down_mm": float(vdown),
                    "view_right_mm": float(vright),
                    "stage_x_target": stage_x,
                    "stage_y_target": stage_y,
                }
            )
            idx += 1

    stage_limit_precheck = _precheck_stage_limits(points, _get_stage_limits(plate))

    return {
        "task_id": params["task_id"],
        "task_type": params["task_type"],
        "plate_type": params["plate_type"],
        "well_name": well_name,
        "objective_name": params["objective_name"],
        "reference": {
            "meaning": f"{well_name}孔左侧观测起始点",
            "a1_start": {
                "x": int(a1_start["x"]),
                "y": int(a1_start["y"]),
            },
            "well_start": {
                "x": int(well_start["x"]),
                "y": int(well_start["y"]),
            },
            "well_diameter_mm": well_diameter_mm,
            "well_gap_mm": well_gap_mm,
            "pitch_mm": pitch_mm,
            "pulses_per_mm": ppm,
            "x_stage_sign_for_view_down": x_sign,
            "y_stage_sign_for_view_right": y_sign,
        },
        "scan_config": {
            "fov_mm": {
                "width": fov_w,
                "height": fov_h,
            },
            "overlap": overlap,
            "step_mm": {
                "width": step_x,
                "height": step_y,
            },
            "point_count": len(points),
        },
        "stage_limit_precheck": stage_limit_precheck,
        "points": points,
    }
