from __future__ import annotations

import textwrap

import pytest

from workflow.detect_api import normalize_detect_result
from workflow.plate_geometry import compute_well_start, parse_well_name, well_name_from_index
from workflow.stage_reciprocation import StageReciprocationController, StageReciprocationError


def test_well_name_round_trip_supports_multi_letter_rows() -> None:
    assert parse_well_name("A1") == (0, 0)
    assert parse_well_name("C6") == (2, 5)
    assert parse_well_name("AA12") == (26, 11)

    assert well_name_from_index(0, 0) == "A1"
    assert well_name_from_index(2, 5) == "C6"
    assert well_name_from_index(26, 11) == "AA12"


def test_compute_well_start_uses_plate_pitch_and_axis_signs() -> None:
    plate_cfg = {
        "rows": 4,
        "cols": 6,
        "a1_start": {"x": 8865800, "y": 6185500},
        "well_diameter_mm": 13.7,
        "well_gap_mm": 3.5,
        "pulses_per_mm": 147500,
        "row_stage_sign": -1,
        "col_stage_sign": -1,
    }

    assert compute_well_start(plate_cfg, "A1") == {
        "x": 8865800,
        "y": 6185500,
        "row_index": 0,
        "col_index": 0,
        "well_name": "A1",
    }
    assert compute_well_start(plate_cfg, "B2") == {
        "x": 6328800,
        "y": 3648500,
        "row_index": 1,
        "col_index": 1,
        "well_name": "B2",
    }


def test_normalize_detect_result_preserves_pickability_fields() -> None:
    raw = {
        "component_count": 2,
        "components": [
            {
                "id": "clone-a",
                "safe_point": [12.4, 34.6],
                "bbox": [1, 2, 3, 4],
                "area_px": 120,
                "score": 0.8,
                "is_valid_for_compensation": "true",
                "touch_image_border": 0,
                "image_border_sides": ["left"],
                "well_border_detected": True,
                "near_well_border": False,
                "distance_to_well_edge_px": 42.5,
                "distance_to_well_edge_mm": 0.85,
                "is_pickable": "yes",
            },
            {"id": "missing-center"},
        ],
    }

    result = normalize_detect_result(raw)

    assert result["clone_count"] == 2
    assert len(result["clones"]) == 1
    clone = result["clones"][0]
    assert clone["clone_id"] == "clone-a"
    assert clone["center_px"] == [12, 35]
    assert clone["bbox"] == [1, 2, 3, 4]
    assert clone["is_valid_for_compensation"] is True
    assert clone["touch_image_border"] is False
    assert clone["image_border_sides"] == ["left"]
    assert clone["well_border_detected"] is True
    assert clone["near_well_border"] is False
    assert clone["distance_to_well_edge_px"] == 42.5
    assert clone["distance_to_well_edge_mm"] == 0.85
    assert clone["is_pickable"] is True


def test_stage_reciprocation_normalize_cfg_builds_fixed_24_well_targets(tmp_path) -> None:
    plates_path = tmp_path / "plates.yaml"
    plates_path.write_text(
        textwrap.dedent(
            """
            plates:
              24-well:
                rows: 4
                cols: 6
                a1_start:
                  x: 8865800
                  y: 6185500
                well_diameter_mm: 13.7
                well_gap_mm: 3.5
                pulses_per_mm: 147500
                row_stage_sign: -1
                col_stage_sign: -1
            """
        ).strip(),
        encoding="utf-8",
    )

    cfg = StageReciprocationController()._normalize_cfg(
        {
            "plates_path": str(plates_path),
            "limit_check_enabled": True,
            "max_cycles": 3,
        }
    )

    assert cfg["plate_type"] == "24-well"
    assert cfg["scan_wells"] == ["B2", "B3", "B4", "C2", "C3", "C4"]
    assert cfg["max_cycles"] == 3
    assert cfg["targets"][0] == {
        "index": 1,
        "well_name": "B2",
        "x": 6328800,
        "y": 3648500,
    }
    assert cfg["targets"][-1]["well_name"] == "C4"


def test_stage_reciprocation_rejects_invalid_limits() -> None:
    controller = StageReciprocationController()
    with pytest.raises(StageReciprocationError, match="min must be smaller"):
        controller._normalize_cfg(
            {
                "limit_check_enabled": True,
                "x_min": 10,
                "x_max": 10,
            }
        )
