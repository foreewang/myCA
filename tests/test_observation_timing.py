"""Timing must survive failed steps without changing hardware lifecycle."""
import json

import pytest

from workflow import timing


def test_measure_accumulates_and_preserves_exception(monkeypatch):
    ticks = iter([1.0, 1.25, 2.0, 2.5])
    monkeypatch.setattr(timing, "perf_counter", lambda: next(ticks))
    values = {}
    with timing.measure(values, "sample"):
        pass
    error = RuntimeError("original error")
    with pytest.raises(RuntimeError) as caught:
        with timing.measure(values, "sample"):
            raise error
    assert caught.value is error
    assert values == {"sample": 750.0}


@pytest.mark.parametrize("failure", [None, "move", "capture", "cancel"])
def test_scan_persists_completed_and_partial_timings(tmp_path, monkeypatch, failure):
    from workflow import scan_executor as scan
    from workflow.task_control import TaskCanceled

    events = []
    error = RuntimeError("hardware failed")

    def move(**kwargs):
        events.append("move")
        kwargs["timings_ms"]["wait_arrival"] = 12.0
        if failure == "move":
            raise error
        return {"timings_ms": kwargs["timings_ms"]}

    def capture(**kwargs):
        events.append("capture")
        if failure == "capture":
            raise error
        return {"saved_path": "example.bmp"}

    def check_cancel(params, stage):
        if failure == "cancel" and stage.startswith("after_capture:"):
            raise TaskCanceled("canceled")

    monkeypatch.setattr(scan, "move_to_absolute", move)
    monkeypatch.setattr(scan, "_check_motion_guard", lambda *args: None)
    monkeypatch.setattr(scan, "open_camera", lambda **kwargs: events.append("open") or object())
    monkeypatch.setattr(scan, "close_camera", lambda cam: events.append("close"))
    monkeypatch.setattr(scan, "capture_with_opened_camera", capture)
    monkeypatch.setattr(scan, "raise_if_cancel_requested", check_cancel)
    path = tmp_path / "scan_result.json"
    params = {
        "task_id": "timing-test", "task_type": "capture", "plate_type": "24-well",
        "well_name": "A1", "objective_name": "4x", "settle_s": 0,
        "scan_output_json": str(path), "save_dir": str(tmp_path),
        "filename_pattern": "{index}.bmp", "device_index": 0,
    }
    point = {
        "index": 1, "row_index": 0, "col_index": 0,
        "view_down_mm": 0, "view_right_mm": 0, "stage_x_target": 1, "stage_y_target": 2,
    }
    plan = {"points": [point], "reference": {}, "scan_config": {}}
    if failure:
        with pytest.raises(TaskCanceled if failure == "cancel" else RuntimeError) as caught:
            scan.execute_scan_capture({"plate": {}}, params, plan)
        if failure != "cancel":
            assert caught.value is error
    else:
        returned = scan.execute_scan_capture({"plate": {}}, params, plan)
        assert returned == json.loads(path.read_text(encoding="utf-8"))

    result = json.loads(path.read_text(encoding="utf-8"))
    status = "canceled" if failure == "cancel" else "failed" if failure else "success"
    record = result["point_timings"][0]
    assert result["status"] == record["status"] == status
    assert record["motion_timings_ms"]["wait_arrival"] == 12.0
    times = record["timings_ms"]
    assert times["point_total"] >= times["move_total"] >= 0
    assert result["timings_ms"]["scan_work"] >= times["point_total"]
    assert "autofocus_total" not in times
    if failure == "move":
        assert events == ["move"]
        assert "capture_total" not in times
    else:
        assert events == ["move", "open", "capture", "close"]
        assert times["capture_total"] >= 0
    assert len(result["captures"]) == (1 if failure in (None, "cancel") else 0)
