"""Fixed observations preserve taught geometry and contain complete camera views."""
from __future__ import annotations

from copy import deepcopy
from itertools import product
from math import hypot, sqrt
from pathlib import Path

import pytest

from workflow.config_validator import load_yaml_unique
from workflow.plate_geometry import all_well_names, compute_well_start
from workflow.scan_modes import has_overlapping_views, resolve_scan_mode
from workflow.scan_planner import plan_single_well_scan


ROOT = Path(__file__).resolve().parents[1]
POINT_NAMES = ["center", "upper_left", "upper_right", "lower_left", "lower_right"]


def _plate() -> dict:
    return {
        "rows": 1, "cols": 1,
        "a1_start": {
            "x": 0, "y": 0, "reference_objective": "4x",
            "reference_fov_mm": {"width": 2.0, "height": 2.0},
        },
        "well_step": {"col": {"x": 0, "y": 0}, "row": {"x": 0, "y": 0}},
        "well_diameter_mm": 10.0,
        "pulses_per_mm": {"x": 10000, "y": 20000},
        "x_stage_sign_for_view_right": -1,
        "y_stage_sign_for_view_down": -1,
        "five_point": {"radial_fraction": 0.5, "edge_margin_mm": 0.5},
        "stage_limits": {"enabled": False},
    }


def _params(**overrides) -> dict:
    return {
        "task_id": "five-points", "task_type": "capture", "plate_type": "test",
        "well_name": "A1", "objective_name": "4x", "overlap": 0.0,
        "fov_mm": {"width": 2.0, "height": 2.0}, **overrides,
    }


@pytest.mark.parametrize("objective,requested,expected", [
    ("4x", None, "five_point"), ("10x", None, "full_coverage"),
    ("20x", None, "full_coverage"), ("4x", "full_coverage", "full_coverage"),
    ("10x", "full_coverage", "full_coverage"), ("4x", "five_point", "five_point"),
])
def test_mode_resolution(objective, requested, expected) -> None:
    assert resolve_scan_mode(objective, requested) == expected


@pytest.mark.parametrize("objective,requested", [
    ("10x", "five_point"), ("20x", "five_point"), ("4x", ""),
    ("4x", "five"), ("4x", False), ("4x", []),
])
def test_unsupported_modes_fail_before_planning(objective, requested) -> None:
    with pytest.raises(ValueError, match="scan.mode"):
        plan_single_well_scan({"plate": {}}, _params(objective_name=objective, scan_mode=requested))


@pytest.mark.parametrize("config,expected", [
    ({}, False), ({"overlap": None}, False), ({"overlap": 0.0}, False),
    ({"overlap": 0.1}, True),
    ({"has_overlapping_views": True, "overlap": 0.0}, True),
    ({"has_overlapping_views": False, "overlap": 0.5}, False),
])
def test_overlap_metadata_precedes_legacy_setting(config, expected) -> None:
    assert has_overlapping_views(config) is expected


@pytest.mark.parametrize("value", [0, 1, "true", "false", None])
def test_overlap_flag_must_be_a_real_boolean(value) -> None:
    with pytest.raises(ValueError, match="bool"):
        has_overlapping_views({"has_overlapping_views": value})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True, "bad"])
def test_legacy_overlap_must_be_finite(value) -> None:
    with pytest.raises(ValueError, match="有限数字"):
        has_overlapping_views({"overlap": value})


@pytest.mark.parametrize("plate_type,expected_d,expected_overlap", [
    ("6-well", 6.059905, False), ("12-well", 3.889087, False),
    ("24-well", 2.701148, True), ("48-well", 1.724008, True),
])
def test_all_production_wells_have_five_safe_quantized_views(plate_type, expected_d, expected_overlap) -> None:
    plate = load_yaml_unique(ROOT / "config/plates.yaml")["plates"][plate_type]
    fov = load_yaml_unique(ROOT / "config/objectives.yaml")["objectives"]["4x"]["fov_mm"]
    radius = plate["well_diameter_mm"] / 2
    margin = plate["five_point"]["edge_margin_mm"]
    for well in all_well_names(plate):
        plan = plan_single_well_scan({"plate": plate}, _params(plate_type=plate_type, well_name=well, fov_mm=fov))
        points = plan["points"]
        config = plan["scan_config"]
        assert config["mode"] == "five_point"
        assert len(points) == config["point_count"] == 5
        assert config["clipped_point_count"] == 0
        assert config["has_overlapping_views"] is expected_overlap
        assert config["five_point"]["axis_offset_mm"] == pytest.approx(expected_d, abs=0.00002)
        assert config["five_point"]["was_inset"] is (plate_type == "48-well")
        assert [p["point_name"] for p in points] == POINT_NAMES
        assert [p["index"] for p in points] == [1, 2, 3, 4, 5]
        assert [(p["row_index"], p["col_index"]) for p in points] == [(1, 1), (0, 0), (0, 2), (2, 0), (2, 2)]
        assert len({(p["stage_x_target"], p["stage_y_target"]) for p in points}) == 5
        assert plan["stage_limit_precheck"]["checked_point_count"] == 5
        assert plan["stage_limit_precheck"]["violations"] == []
        center = plan["reference"]["well_center"]
        start = compute_well_start(plate, well)
        assert center["x"] == pytest.approx(start["x"] - (radius - 1.61) * 65536)
        assert center["y"] == start["y"]
        for point in points:
            dx = (point["stage_x_target"] - center["x"]) / (-65536)
            dy = (point["stage_y_target"] - center["y"]) / (-131072)
            for sx, sy in product((-1, 1), repeat=2):
                assert hypot(dx + sx * fov["width"] / 2, dy + sy * fov["height"] / 2) <= radius - margin
            assert point["view_right_mm"] == (point["stage_x_target"] - start["x"]) / -65536
            assert point["view_down_mm"] == (point["stage_y_target"] - start["y"]) / -131072
        assert points[1]["stage_x_target"] > points[0]["stage_x_target"] > points[2]["stage_x_target"]
        assert points[1]["stage_y_target"] > points[0]["stage_y_target"] > points[3]["stage_y_target"]


def test_center_uses_taught_fov_even_if_capture_fov_changes() -> None:
    plate = _plate()
    first = plan_single_well_scan({"plate": plate}, _params())
    second = plan_single_well_scan({"plate": plate}, _params(fov_mm={"width": 1.0, "height": 1.5}))
    assert first["reference"]["well_center"] == second["reference"]["well_center"] == {"x": -40000, "y": 0}
    assert first["points"][0] == second["points"][0]


def test_rectangular_view_is_inset_using_both_dimensions() -> None:
    plate = _plate()
    plate["five_point"]["radial_fraction"] = 0.9
    plan = plan_single_well_scan({"plate": plate}, _params(fov_mm={"width": 4.0, "height": 1.0}))
    assert plan["scan_config"]["five_point"]["was_inset"] is True
    center = plan["reference"]["well_center"]
    for point in plan["points"]:
        x = (point["stage_x_target"] - center["x"]) / 10000
        y = (point["stage_y_target"] - center["y"]) / 20000
        for sx, sy in product((-1, 1), repeat=2):
            assert hypot(x + sx * 2.0, y + sy * 0.5) <= 4.5


def test_overlap_is_measured_after_rounding_not_taken_from_grid_overlap() -> None:
    plate = _plate()
    plate["five_point"]["radial_fraction"] = 0.7
    for overlap in (0.0, 0.9):
        plan = plan_single_well_scan({"plate": plate}, _params(overlap=overlap))
        assert plan["scan_config"]["has_overlapping_views"] is False
    plate["five_point"]["radial_fraction"] = 0.4
    plan = plan_single_well_scan({"plate": plate}, _params(overlap=0.0))
    assert plan["scan_config"]["has_overlapping_views"] is True


def test_edge_touching_views_are_not_positive_area_overlap() -> None:
    plate = _plate()
    plate["well_diameter_mm"] = 20.0
    plate["five_point"]["radial_fraction"] = 2.0 * sqrt(2) / 10.0
    plan = plan_single_well_scan({"plate": plate}, _params())
    assert plan["scan_config"]["has_overlapping_views"] is False


def test_single_out_of_limits_point_rejects_whole_well() -> None:
    plate = _plate()
    plate["stage_limits"] = {
        "enabled": True, "x_min": -1000000, "x_max": 1000000,
        "y_min": -1000000, "y_max": 10000, "safety_margin": 0,
    }
    with pytest.raises(ValueError, match="扫描路径越出位移台安全范围"):
        plan_single_well_scan({"plate": plate}, _params())


def test_center_view_that_does_not_fit_is_rejected() -> None:
    plate = _plate()
    plate["well_diameter_mm"] = 5.2
    with pytest.raises(ValueError, match="中心视野"):
        plan_single_well_scan({"plate": plate}, _params(fov_mm={"width": 3.22, "height": 3.22}))


def test_five_positions_must_remain_unique_after_quantization() -> None:
    plate = _plate()
    plate["pulses_per_mm"] = {"x": 1.0, "y": 1.0}
    plate["five_point"]["radial_fraction"] = 0.01
    with pytest.raises(ValueError, match="不唯一"):
        plan_single_well_scan({"plate": plate}, _params())


@pytest.mark.parametrize("path,value,error", [
    (("five_point", "radial_fraction"), 0, "radial_fraction"),
    (("five_point", "radial_fraction"), 1, "radial_fraction"),
    (("five_point", "radial_fraction"), float("nan"), "radial_fraction"),
    (("five_point", "edge_margin_mm"), -1, "edge_margin_mm"),
    (("five_point", "edge_margin_mm"), 0, "edge_margin_mm"),
    (("five_point", "edge_margin_mm"), float("inf"), "edge_margin_mm"),
    (("well_diameter_mm",), float("nan"), "well_diameter_mm"),
    (("well_diameter_mm",), -1, "well_diameter_mm"),
    (("pulses_per_mm", "x"), float("inf"), "pulses_per_mm.x"),
    (("a1_start", "reference_objective"), "10x", "reference_objective"),
    (("a1_start", "reference_fov_mm", "width"), None, "reference_fov_mm.width"),
    (("a1_start", "reference_fov_mm", "height"), float("nan"), "reference_fov_mm.height"),
    (("a1_start", "x"), float("inf"), "a1_start.x"),
    (("x_stage_sign_for_view_right",), 1.5, "x_stage_sign_for_view_right"),
])
def test_invalid_geometry_values_are_rejected(path, value, error) -> None:
    plate = _plate()
    owner = plate
    for key in path[:-1]:
        owner = owner[key]
    owner[path[-1]] = value
    with pytest.raises(ValueError, match=error):
        plan_single_well_scan({"plate": plate}, _params())


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), True])
def test_invalid_capture_fov_is_rejected(value) -> None:
    with pytest.raises(ValueError, match="fov_mm.width"):
        plan_single_well_scan({"plate": _plate()}, _params(fov_mm={"width": value, "height": 2.0}))


def test_missing_teaching_metadata_only_blocks_five_point_mode() -> None:
    plate = deepcopy(_plate())
    del plate["a1_start"]["reference_fov_mm"]
    with pytest.raises(ValueError, match="reference_fov_mm"):
        plan_single_well_scan({"plate": plate}, _params())
    old = plan_single_well_scan({"plate": plate}, _params(scan_mode="full_coverage"))
    assert old["scan_config"]["mode"] == "full_coverage"
    assert old["points"][0]["stage_x_target"] == plate["a1_start"]["x"]
    ten_x = plan_single_well_scan({"plate": plate}, _params(objective_name="10x"))
    assert ten_x["scan_config"]["mode"] == "full_coverage"
