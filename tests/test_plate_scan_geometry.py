"""Scan path uses inner diameter and taught well steps, not diameter+gap."""
from __future__ import annotations

from pathlib import Path

import pytest

from workflow.config_validator import load_yaml_unique
from workflow.plate_geometry import compute_well_start, get_plate_pitch_mm, get_well_step_pulses
from workflow.scan_planner import plan_single_well_scan


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _scan_params(well_name: str = "A1", fov: float = 3.22, overlap: float = 0.1) -> dict:
    return {
        "task_id": "inner-scan",
        "task_type": "capture",
        "plate_type": "24-well",
        "well_name": well_name,
        "objective_name": "4x",
        "fov_mm": {"width": fov, "height": fov},
        "overlap": overlap,
    }


def test_compute_well_start_requires_taught_step() -> None:
    plate = {
        "rows": 2,
        "cols": 2,
        "a1_start": {"x": 100, "y": 200},
        "well_diameter_mm": 10.0,
        "well_gap_mm": 2.0,
        "pulses_per_mm": {"x": 100, "y": 200},
    }
    with pytest.raises(KeyError, match="well_step"):
        compute_well_start(plate, "B2")


def test_compute_well_start_uses_signed_pulse_steps_not_signs() -> None:
    plate = {
        "rows": 2,
        "cols": 3,
        "a1_start": {"x": 1000, "y": 2000},
        "well_diameter_mm": 10.0,
        "well_step": {"col": {"x": -50, "y": 1}, "row": {"x": 2, "y": -80}},
        "pulses_per_mm": {"x": 10, "y": 20},
    }
    start = compute_well_start(plate, "B3")
    assert start["x"] == 1000 + 2 * -50 + 1 * 2
    assert start["y"] == 2000 + 2 * 1 + 1 * -80
    pitch = get_plate_pitch_mm(plate)
    assert pitch == {"x": 5.0, "y": 4.0}


def test_scan_visits_center_then_down_then_up_from_well_start() -> None:
    plate = {
        "rows": 1,
        "cols": 1,
        "a1_start": {"x": 0, "y": 0},
        "well_diameter_mm": 10.0,
        "well_step": {"col": {"x": 0, "y": 0}, "row": {"x": 0, "y": 0}},
        "pulses_per_mm": {"x": 100, "y": 100},
        "x_stage_sign_for_view_right": 1,
        "y_stage_sign_for_view_down": 1,
        "stage_limits": {"enabled": False},
    }
    plan = plan_single_well_scan({"plate": plate}, _scan_params(fov=2.0, overlap=0.0))
    first = plan["points"][0]
    assert first["view_right_mm"] == 0.0
    assert first["view_down_mm"] == 0.0
    assert first["stage_x_target"] == 0
    assert first["stage_y_target"] == 0

    row_order = []
    last_row = None
    for point in plan["points"]:
        if point["row_index"] != last_row:
            row_order.append(point["view_down_mm"])
            last_row = point["row_index"]
    assert row_order == [0.0, 2.0, 4.0, -2.0, -4.0]

    coords = {(round(p["view_right_mm"], 6), round(p["view_down_mm"], 6)) for p in plan["points"]}
    assert (0.0, 0.0) in coords
    assert all(abs(vdown) <= 4.0 + 1e-6 for _, vdown in coords)


def test_scan_radius_insets_by_half_fov() -> None:
    plate = {
        "rows": 1,
        "cols": 1,
        "a1_start": {"x": 0, "y": 0},
        "well_diameter_mm": 10.0,
        "well_step": {"col": {"x": 0, "y": 0}, "row": {"x": 0, "y": 0}},
        "pulses_per_mm": {"x": 100, "y": 100},
        "x_stage_sign_for_view_right": 1,
        "y_stage_sign_for_view_down": 1,
        "stage_limits": {"enabled": False},
    }
    plan = plan_single_well_scan({"plate": plate}, _scan_params(fov=2.0, overlap=0.0))
    assert plan["scan_config"]["fov_inset_mm"] == 1.0
    assert plan["scan_config"]["scan_radius_mm"] == 4.0
    assert max(abs(p["view_right_mm"]) for p in plan["points"]) <= 8.0 + 1e-6
    assert max(abs(p["view_down_mm"]) for p in plan["points"]) <= 4.0 + 1e-6


def test_scan_planner_defaults_missing_overlap() -> None:
    plate = {
        "rows": 1,
        "cols": 1,
        "a1_start": {"x": 0, "y": 0},
        "well_diameter_mm": 10.0,
        "well_step": {"col": {"x": 0, "y": 0}, "row": {"x": 0, "y": 0}},
        "pulses_per_mm": {"x": 100, "y": 100},
        "x_stage_sign_for_view_right": 1,
        "y_stage_sign_for_view_down": 1,
        "stage_limits": {"enabled": False},
    }
    params = _scan_params(fov=2.0)
    del params["overlap"]
    plan = plan_single_well_scan({"plate": plate}, params)
    assert plan["scan_config"]["overlap"] == 0.0


def test_scan_planner_clips_points_above_safe_y() -> None:
    plate = {
        "rows": 1,
        "cols": 1,
        "a1_start": {"x": 0, "y": 100},
        "well_diameter_mm": 10.0,
        "well_step": {"col": {"x": 0, "y": 0}, "row": {"x": 0, "y": 0}},
        "pulses_per_mm": {"x": 10, "y": 10},
        "x_stage_sign_for_view_right": -1,
        "y_stage_sign_for_view_down": -1,
        "stage_limits": {
            "enabled": True,
            "x_min": -10000,
            "x_max": 10000,
            "y_min": -10000,
            "y_max": 130,
            "safety_margin": 0,
        },
    }
    # view up = negative vdown, y_sign_down=-1 → stage_y = 100 + 10*|vdown|
    plan = plan_single_well_scan({"plate": plate}, _scan_params(fov=2.0, overlap=0.0))
    assert plan["scan_config"]["clipped_point_count"] > 0
    assert plan["scan_config"]["point_count"] == plan["scan_config"]["planned_point_count"] - plan["scan_config"]["clipped_point_count"]
    assert all(p["stage_y_target"] <= 130 for p in plan["points"])
    assert plan["stage_limit_precheck"]["violations"] == []


def test_production_plates_use_inner_diameter_and_taught_steps() -> None:
    plates = load_yaml_unique(PROJECT_ROOT / "config" / "plates.yaml")["plates"]
    assert compute_well_start(plates["24-well"], "A6")["x"] == 6117668 + 5 * -1265563
    assert compute_well_start(plates["24-well"], "D1") == {
        "x": 6117668,
        "y": 221468,
        "row_index": 3,
        "col_index": 0,
        "well_name": "D1",
    }
    # A6/D1 taught points round into well_step by 1 pulse; diagonal is archive-only.
    assert plates["48-well"]["well_teach"]["diagonal"] == {"well": "F8", "x": -151997, "y": -344640}
    assert compute_well_start(plates["48-well"], "F8") == {
        "x": -131853,
        "y": -344641,
        "row_index": 5,
        "col_index": 7,
        "well_name": "F8",
    }

    plan = plan_single_well_scan(
        {"plate": plates["48-well"]},
        {
            **_scan_params(well_name="A1", fov=3.22, overlap=0.1),
            "plate_type": "48-well",
        },
    )
    assert plan["scan_config"]["scan_radius_mm"] == pytest.approx((10.28811523 - 3.22) / 2.0)
    assert plan["scan_config"]["point_count"] > 0
    y_hi = 9500000 - 131072
    y_lo = -1000000 + 131072
    assert all(p["stage_y_target"] <= y_hi for p in plan["points"])
    assert plan["stage_limit_precheck"]["violations"] == []

    last_row_10x = plan_single_well_scan(
        {"plate": plates["48-well"]},
        {
            **_scan_params(well_name="F1", fov=1.31, overlap=0.1),
            "plate_type": "48-well",
            "objective_name": "10x",
        },
    )
    assert last_row_10x["scan_config"]["clipped_point_count"] == 0
    assert all(y_lo <= p["stage_y_target"] <= y_hi for p in last_row_10x["points"])
    assert last_row_10x["stage_limit_precheck"]["violations"] == []
