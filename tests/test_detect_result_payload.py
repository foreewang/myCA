"""Preserve coverage for the separate contour-payload reduction change."""
from copy import deepcopy
import json

import pytest
from PIL import Image


@pytest.mark.parametrize("has_pickable", [True, False])
def test_detect_result_keeps_polygon_flag_without_contour_arrays(tmp_path, monkeypatch, has_pickable):
    from workflow import detect_executor

    image_path = tmp_path / "source.bmp"
    Image.new("L", (100, 100)).save(image_path)
    raw = {"schema_version": 1, "clones": [
        {"clone_id": "C01", "center_px": [60, 40], "is_pickable": has_pickable,
         "raw": {"contour_points": [[1, 2], [3, 4], [5, 6]]}},
        {"clone_id": "C02", "center_px": [20, 40], "is_pickable": False}]}
    monkeypatch.setattr(detect_executor, "run_detect_on_image", lambda *a, **k: deepcopy(raw))
    def dedupe(images, **kwargs):
        groups = []
        for image in images:
            for clone in image["clones"]:
                clone["global_clone_id"] = clone["clone_id"]
                groups.append({"global_clone_id": clone["clone_id"],
                               "source_detections": [clone["source_detection_id"]]})
        return {"unique_clones": groups, "metadata": {}}
    monkeypatch.setattr(detect_executor, "dedupe_well_clones", dedupe)
    params = {"task_id": "test", "plate_type": "12-well", "well_name": "B2", "objective_name": "4x",
              "detect_output_json": str(tmp_path / "detect_result.json")}
    result = detect_executor.execute_detect_on_scan_result({"task": {"detect": {"save_overlay": False}}}, params, {
        "scan_config": {"fov_mm": {"width": 1, "height": 1}},
        "captures": [{"index": 31, "row_index": 0, "col_index": 0,
                      "stage_x_target": 100, "stage_y_target": 200,
                      "capture_result": {"saved_path": str(image_path)}}]})
    assert json.loads((tmp_path / "detect_result.json").read_text(encoding="utf-8")) == result
    assert len(result["images"][0]["clones"]) == 2
    assert result["images"][0]["clones"][0]["has_polygon"] is True
    assert '"contour_points"' not in json.dumps(result)
