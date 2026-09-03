from __future__ import annotations

import pytest

from tools.stage_reciprocation_accuracy import (
    TestDefinitionError as StageAccuracyDefinitionError,
    _validate_test_definition,
    summarize_records,
)


LIMITS = {
    "enabled": True,
    "x_min": 0,
    "x_max": 1000,
    "y_min": 0,
    "y_max": 2000,
    "safety_margin": 100,
}


def test_validate_test_definition_enforces_axis_and_safe_bounds() -> None:
    bounds = _validate_test_definition(
        axis="x",
        point_a={"x": 200, "y": 500},
        point_b={"x": 800, "y": 500},
        limits=LIMITS,
    )
    assert bounds == {"x_min": 100, "x_max": 900, "y_min": 100, "y_max": 1900}

    with pytest.raises(StageAccuracyDefinitionError, match="point-a-y"):
        _validate_test_definition(
            axis="x",
            point_a={"x": 200, "y": 500},
            point_b={"x": 800, "y": 600},
            limits=LIMITS,
        )

    with pytest.raises(StageAccuracyDefinitionError, match="超出安全范围"):
        _validate_test_definition(
            axis="xy",
            point_a={"x": 50, "y": 500},
            point_b={"x": 800, "y": 600},
            limits=LIMITS,
        )


def test_summarize_records_uses_measurement_success_rows_only() -> None:
    records = [
        {"phase": "warmup", "endpoint": "A", "status": "success", "error_x_pulse": 99, "error_y_pulse": 99},
        {"phase": "measurement", "endpoint": "B", "status": "success", "error_x_pulse": 2, "error_y_pulse": -4},
        {"phase": "measurement", "endpoint": "A", "status": "success", "error_x_pulse": -2, "error_y_pulse": 4},
        {"phase": "measurement", "endpoint": "B", "status": "success", "error_x_pulse": 4, "error_y_pulse": -2},
        {"phase": "measurement", "endpoint": "A", "status": "success", "error_x_pulse": 0, "error_y_pulse": 2},
        {"phase": "measurement", "endpoint": "A", "status": "failed", "error_x_pulse": 999, "error_y_pulse": 999},
    ]

    summary = summarize_records(records, x_ppm=100, y_ppm=200)

    assert summary["measurement_move_count"] == 4
    assert summary["completed_cycles"] == 2
    assert summary["endpoints"]["B"]["x"]["mean_error_pulse"] == 3
    assert summary["endpoints"]["B"]["x"]["range_pulse"] == 2
    assert summary["endpoints"]["A"]["y"]["mean_error_mm"] == pytest.approx(0.015)
    assert summary["overall"]["x"]["max_abs_error_pulse"] == 4
