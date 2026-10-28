"""根据孔位几何、视野和重叠率生成单孔扫描点位计划。"""
from __future__ import annotations

from collections.abc import Mapping
from math import hypot, isfinite, sqrt
from typing import Dict, List, Any

from workflow.plate_geometry import (
    compute_well_start,
    get_a1_start,
    get_axis_pulses_per_mm,
    get_plate_pitch_mm,
    get_view_signs,
    get_well_step_pulses,
    require_number,
)
from workflow.platform_defaults import DEFAULT_SCAN_OVERLAP
from workflow.scan_modes import resolve_scan_mode


def _row_values(step_y: float, radius: float) -> List[float]:
    """生成扫描行的视野向下偏移，顺序为中心行、下侧各行，再上侧各行。

    点集与按半径铺满圆的网格相同；访问顺序从示教起点（中心行左端）出发，
    先连续扫完下半孔再扫上半孔，避免在上下两侧来回跳跃。
    """
    downs = [0.0]
    k = 1
    while True:
        y = round(k * step_y, 6)
        if y > radius:
            break
        downs.append(y)
        k += 1
    ups = [-y for y in downs[1:]]
    return downs + ups


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


def _safe_stage_box(stage_limits: Dict[str, Any]) -> Dict[str, int] | None:
    if not stage_limits["enabled"]:
        return None
    required = ["x_min", "x_max", "y_min", "y_max"]
    for key in required:
        if stage_limits[key] is None:
            raise ValueError(f"stage_limits.enabled=true，但缺少 {key}")
    margin = int(stage_limits["safety_margin"])
    return {
        "x_min_safe": int(stage_limits["x_min"]) + margin,
        "x_max_safe": int(stage_limits["x_max"]) - margin,
        "y_min_safe": int(stage_limits["y_min"]) + margin,
        "y_max_safe": int(stage_limits["y_max"]) - margin,
    }


def _in_safe_box(x: int, y: int, box: Dict[str, int]) -> bool:
    return (
        box["x_min_safe"] <= x <= box["x_max_safe"]
        and box["y_min_safe"] <= y <= box["y_max_safe"]
    )


def _clip_points_to_safe_box(
    points: List[Dict[str, Any]],
    box: Dict[str, int] | None,
) -> tuple[List[Dict[str, Any]], int]:
    if box is None:
        return points, 0
    kept: List[Dict[str, Any]] = []
    clipped = 0
    for point in points:
        x = int(point["stage_x_target"])
        y = int(point["stage_y_target"])
        if _in_safe_box(x, y, box):
            kept.append(dict(point))
        else:
            clipped += 1
    for index, point in enumerate(kept, start=1):
        point["index"] = index
    return kept, clipped


def _finite_number(value: Any, name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} 必须为有限数字")
    number = require_number(value, name)
    if not isfinite(number):
        raise ValueError(f"{name} 必须为有限数字")
    if positive and number <= 0:
        raise ValueError(f"{name} 必须大于 0")
    return number


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} 必须为配置映射")
    return value


def _five_point_stage_limits(plate: Dict[str, Any]) -> Dict[str, Any]:
    cfg = _mapping(plate.get("stage_limits", {}), "stage_limits")
    enabled = cfg.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("stage_limits.enabled 必须为 bool")
    if enabled:
        for key in ("x_min", "x_max", "y_min", "y_max"):
            _finite_number(cfg.get(key), f"stage_limits.{key}")
        margin = _finite_number(cfg.get("safety_margin", 0), "stage_limits.safety_margin")
        if margin < 0:
            raise ValueError("stage_limits.safety_margin 不能为负数")
    limits = _get_stage_limits(plate)
    box = _safe_stage_box(limits)
    if box is not None and (
        box["x_min_safe"] > box["x_max_safe"] or box["y_min_safe"] > box["y_max_safe"]
    ):
        raise ValueError("stage_limits 扣除安全边界后无有效范围")
    return limits


def _plan_five_point_scan(ctx: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
    """Place five complete rectangular views inside the taught physical well."""
    plate = ctx["plate"]
    well_name = params["well_name"]
    a1_cfg = _mapping(plate.get("a1_start"), "a1_start")
    if a1_cfg.get("reference_objective") != "4x":
        raise ValueError("five_point 需要 a1_start.reference_objective=4x 的示教基准")
    reference_fov = _mapping(a1_cfg.get("reference_fov_mm"), "a1_start.reference_fov_mm")
    reference_w = _finite_number(reference_fov.get("width"), "a1_start.reference_fov_mm.width", positive=True)
    reference_h = _finite_number(reference_fov.get("height"), "a1_start.reference_fov_mm.height", positive=True)
    for axis in ("x", "y"):
        _finite_number(a1_cfg.get(axis), f"a1_start.{axis}")
    raw_step = _mapping(plate.get("well_step"), "well_step")
    for direction in ("col", "row"):
        step = _mapping(raw_step.get(direction), f"well_step.{direction}")
        for axis in ("x", "y"):
            _finite_number(step.get(axis), f"well_step.{direction}.{axis}")

    diameter = _finite_number(plate.get("well_diameter_mm"), "well_diameter_mm", positive=True)
    if reference_w >= diameter:
        raise ValueError("a1_start.reference_fov_mm.width 必须小于孔内径")
    fov = _mapping(params.get("fov_mm"), "fov_mm")
    fov_w = _finite_number(fov.get("width"), "fov_mm.width", positive=True)
    fov_h = _finite_number(fov.get("height"), "fov_mm.height", positive=True)
    settings = _mapping(plate.get("five_point"), "five_point")
    fraction = _finite_number(settings.get("radial_fraction"), "five_point.radial_fraction")
    if not 0.0 < fraction < 1.0:
        raise ValueError("five_point.radial_fraction 必须满足 0 < value < 1")
    margin = _finite_number(settings.get("edge_margin_mm"), "five_point.edge_margin_mm", positive=True)
    overlap = _finite_number(
        DEFAULT_SCAN_OVERLAP if params.get("overlap") is None else params["overlap"], "overlap"
    )
    if not 0.0 <= overlap < 1.0:
        raise ValueError("overlap 必须满足 0 <= overlap < 1")

    well_start = compute_well_start(plate, well_name)
    a1_start = get_a1_start(plate)
    x_ppm, y_ppm = get_axis_pulses_per_mm(plate)
    x_ppm = _finite_number(x_ppm, "pulses_per_mm.x", positive=True)
    y_ppm = _finite_number(y_ppm, "pulses_per_mm.y", positive=True)
    for key in ("x_stage_sign_for_view_right", "y_stage_sign_for_view_down"):
        if _finite_number(plate.get(key), key) not in (-1, 1):
            raise ValueError(f"{key} 必须为 +1 或 -1")
    x_sign, y_sign = get_view_signs(plate)

    radius = diameter / 2.0
    half_w, half_h = fov_w / 2.0, fov_h / 2.0
    # Bound one rounding operation per absolute stage coordinate, in physical mm.
    quantization_margin = _finite_number(hypot(0.5 / x_ppm, 0.5 / y_ppm), "quantization_margin_mm")
    usable_radius = radius - margin - quantization_margin
    if usable_radius <= hypot(half_w, half_h):
        raise ValueError("five_point 中心视野及安全余量无法完整放入孔内，不能生成五个独立点")
    nominal_d = radius * fraction / sqrt(2.0)
    # Solve (d + half_w)^2 + (d + half_h)^2 <= usable_radius^2.
    # Scale the discriminant to avoid squaring large physical configuration values.
    skew = (half_w - half_h) / usable_radius
    limit_d = usable_radius * sqrt(2.0 - skew * skew) / 2.0 - half_w / 2.0 - half_h / 2.0
    d = _finite_number(min(nominal_d, limit_d), "five_point.axis_offset_mm", positive=True)
    center_shift = radius - reference_w / 2.0
    center_x = _finite_number(well_start["x"] + x_sign * center_shift * x_ppm, "well_center.x")
    center_y = float(well_start["y"])
    positions = (
        ("center", 1, 1, 0.0, 0.0),
        ("upper_left", 0, 0, -d, -d),
        ("upper_right", 0, 2, d, -d),
        ("lower_left", 2, 0, -d, d),
        ("lower_right", 2, 2, d, d),
    )
    points: List[Dict[str, Any]] = []
    clearances = []
    for index, (name, row, col, dx, dy) in enumerate(positions, start=1):
        target_x = int(round(_finite_number(center_x + x_sign * dx * x_ppm, f"{name}.x")))
        target_y = int(round(_finite_number(center_y + y_sign * dy * y_ppm, f"{name}.y")))
        actual_dx = (target_x - center_x) / (x_sign * x_ppm)
        actual_dy = (target_y - center_y) / (y_sign * y_ppm)
        clearance = radius - hypot(abs(actual_dx) + half_w, abs(actual_dy) + half_h)
        if clearance < margin:
            raise ValueError(f"five_point {name} 取整后的完整视野侵入孔边安全余量")
        clearances.append(clearance)
        points.append({
            "index": index,
            "point_name": name,
            "row_index": row,
            "col_index": col,
            # Preserve the existing well_start origin for all consumers.
            "view_right_mm": (target_x - well_start["x"]) / (x_sign * x_ppm),
            "view_down_mm": (target_y - well_start["y"]) / (y_sign * y_ppm),
            "stage_x_target": target_x,
            "stage_y_target": target_y,
        })
    if len({(p["stage_x_target"], p["stage_y_target"]) for p in points}) != 5:
        raise ValueError("five_point 五个点在脉冲取整后不唯一，无法执行固定五点观察")

    stage_limits = _five_point_stage_limits(plate)
    # Fixed-point sampling must never silently discard one of its five views.
    precheck = _precheck_stage_limits(points, stage_limits)
    precheck.update({"clipped_point_count": 0, "planned_point_count": 5})
    actual_overlap = any(
        abs(a["stage_x_target"] - b["stage_x_target"]) / x_ppm < fov_w
        and abs(a["stage_y_target"] - b["stage_y_target"]) / y_ppm < fov_h
        for index, a in enumerate(points) for b in points[index + 1:]
    )
    reference = {
        "meaning": f"{well_name}孔左侧观测起始点（画面右侧刚贴内径内壁）",
        "a1_start": {"x": a1_start["x"], "y": a1_start["y"]},
        "well_start": {"x": well_start["x"], "y": well_start["y"]},
        "reference_objective": "4x",
        "reference_fov_mm": {"width": reference_w, "height": reference_h},
        # The inferred physical center is not rounded to stage pulses.
        "well_center": {"x": center_x, "y": center_y},
        "well_diameter_mm": diameter,
        "well_step": get_well_step_pulses(plate),
        "pitch_mm": get_plate_pitch_mm(plate),
        "pulses_per_mm": {"x": x_ppm, "y": y_ppm},
        "x_stage_sign_for_view_right": x_sign,
        "y_stage_sign_for_view_down": y_sign,
    }
    if plate.get("well_gap_mm") is not None:
        reference["well_gap_mm"] = _finite_number(plate["well_gap_mm"], "well_gap_mm")
    return {
        "task_id": params["task_id"],
        "task_type": params["task_type"],
        "plate_type": params["plate_type"],
        "well_name": well_name,
        "objective_name": params["objective_name"],
        "reference": reference,
        "scan_config": {
            "mode": "five_point",
            "fov_mm": {"width": fov_w, "height": fov_h},
            "overlap": overlap,
            "has_overlapping_views": actual_overlap,
            "five_point": {
                "radial_fraction": fraction,
                "edge_margin_mm": margin,
                "nominal_axis_offset_mm": nominal_d,
                "axis_offset_mm": d,
                "radial_distance_mm": sqrt(2.0) * d,
                "actual_radial_fraction": sqrt(2.0) * d / radius,
                "was_inset": d < nominal_d,
                "quantization_margin_mm": quantization_margin,
                "min_corner_clearance_mm": min(clearances),
            },
            "planned_point_count": 5,
            "clipped_point_count": 0,
            "point_count": 5,
        },
        "stage_limit_precheck": precheck,
        "points": points,
    }


def plan_single_well_scan(ctx: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve the objective's observation mode before planning any motion."""
    mode = resolve_scan_mode(params["objective_name"], params.get("scan_mode"))
    if mode == "five_point":
        return _plan_five_point_scan(ctx, params)
    return _plan_full_coverage_scan(ctx, params)


def _plan_full_coverage_scan(ctx: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
    """为单个培养孔生成完整扫描计划。"""
    plate = ctx["plate"]
    well_name = params["well_name"]

    well_start = compute_well_start(plate, well_name)
    a1_start = get_a1_start(plate)
    x_ppm, y_ppm = get_axis_pulses_per_mm(plate)
    x_sign_for_right, y_sign_for_down = get_view_signs(plate)

    well_diameter_mm = require_number(plate.get("well_diameter_mm"), "well_diameter_mm")
    well_gap_mm = plate.get("well_gap_mm")
    well_gap_value = (
        require_number(well_gap_mm, "well_gap_mm") if well_gap_mm is not None else None
    )
    pitch_mm = get_plate_pitch_mm(plate)
    well_step = get_well_step_pulses(plate)

    fov_w = require_number(params["fov_mm"]["width"], "fov_mm.width")
    fov_h = require_number(params["fov_mm"]["height"], "fov_mm.height")
    overlap_raw = params.get("overlap")
    if overlap_raw is None:
        overlap_raw = DEFAULT_SCAN_OVERLAP
    overlap = require_number(overlap_raw, "overlap")

    if fov_w <= 0 or fov_h <= 0:
        raise ValueError(f"视野尺寸必须大于 0，当前 fov_mm=({fov_w}, {fov_h})")
    if not (0.0 <= overlap < 1.0):
        raise ValueError(f"overlap 必须满足 0 <= overlap < 1，当前为 {overlap}")

    step_x = fov_w * (1.0 - overlap)
    step_y = fov_h * (1.0 - overlap)

    if step_x <= 0 or step_y <= 0:
        raise ValueError(f"扫描步长必须大于 0，当前 step_mm=({step_x}, {step_y})")

    # 起点已是「画面右侧刚贴内壁」，相机中心已内收半个视野。
    # 扫描圆是相机中心轨迹，半径 = 内径/2 − max(FOV宽, FOV高)/2。
    fov_inset_mm = max(fov_w, fov_h) / 2.0
    radius = well_diameter_mm / 2.0 - fov_inset_mm
    if radius <= 0:
        raise ValueError(
            "内径在扣除当前视野半宽后无法形成扫描圆："
            f" well_diameter_mm={well_diameter_mm}, fov_mm=({fov_w}, {fov_h})"
        )
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
            # 标准坐标映射：图像左右对应 X/电机 1，图像上下对应 Y/电机 2。
            stage_x = int(round(well_start["x"] + x_sign_for_right * vright * x_ppm))
            stage_y = int(round(well_start["y"] + y_sign_for_down * vdown * y_ppm))

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

    stage_limits = _get_stage_limits(plate)
    safe_box = _safe_stage_box(stage_limits)
    if safe_box is not None and not _in_safe_box(int(well_start["x"]), int(well_start["y"]), safe_box):
        raise ValueError(
            f"{well_name} 观测起始点越出位移台安全范围："
            f" ({well_start['x']}, {well_start['y']})，"
            f" 安全范围 X[{safe_box['x_min_safe']}, {safe_box['x_max_safe']}]"
            f" Y[{safe_box['y_min_safe']}, {safe_box['y_max_safe']}]"
        )

    planned_point_count = len(points)
    points, clipped_count = _clip_points_to_safe_box(points, safe_box)
    if not points:
        raise ValueError(
            f"{well_name} 扫描点在扣除位移台安全范围后为空。"
            f" 原规划 {planned_point_count} 个点全部越界。"
        )

    stage_limit_precheck = _precheck_stage_limits(points, stage_limits)
    stage_limit_precheck["clipped_point_count"] = clipped_count
    stage_limit_precheck["planned_point_count"] = planned_point_count

    reference = {
        "meaning": f"{well_name}孔左侧观测起始点（画面右侧刚贴内径内壁）",
        "a1_start": {
            "x": int(a1_start["x"]),
            "y": int(a1_start["y"]),
        },
        "well_start": {
            "x": int(well_start["x"]),
            "y": int(well_start["y"]),
        },
        "well_diameter_mm": well_diameter_mm,
        "well_step": well_step,
        "pitch_mm": pitch_mm,
        "pulses_per_mm": {
            "x": x_ppm,
            "y": y_ppm,
        },
        "x_stage_sign_for_view_right": x_sign_for_right,
        "y_stage_sign_for_view_down": y_sign_for_down,
    }
    if well_gap_value is not None:
        reference["well_gap_mm"] = well_gap_value

    return {
        "task_id": params["task_id"],
        "task_type": params["task_type"],
        "plate_type": params["plate_type"],
        "well_name": well_name,
        "objective_name": params["objective_name"],
        "reference": reference,
        "scan_config": {
            "mode": "full_coverage",
            "fov_mm": {
                "width": fov_w,
                "height": fov_h,
            },
            "overlap": overlap,
            "step_mm": {
                "width": step_x,
                "height": step_y,
            },
            "fov_inset_mm": fov_inset_mm,
            "scan_radius_mm": radius,
            "planned_point_count": planned_point_count,
            "clipped_point_count": clipped_count,
            "point_count": len(points),
        },
        "stage_limit_precheck": stage_limit_precheck,
        "points": points,
    }
