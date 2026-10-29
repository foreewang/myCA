"""Observation JPEG copies preserve originals and survive partial scans."""
import json
from pathlib import Path

import pytest
from PIL import Image, JpegImagePlugin

from workflow import camera_executor, overlay_preview, run_task, scan_executor as scan
from workflow.task_control import TaskCanceled


@pytest.fixture
def observation(tmp_path, monkeypatch):
    originals = {}

    def capture(**kwargs):
        index = kwargs["format_kwargs"]["index"]
        path = Path(kwargs["save_dir"]) / f"照片_{index:03d}.bmp"
        path.parent.mkdir(parents=True, exist_ok=True)
        mode, color = ("L", 128) if index % 2 else ("RGB", (30, 190, 70))
        Image.new(mode, (137, 91), color).save(path)
        originals[path] = path.read_bytes()
        return {"saved_path": str(path)}

    monkeypatch.setattr(scan, "move_to_absolute", lambda **kwargs: {})
    monkeypatch.setattr(scan, "_check_motion_guard", lambda *args: None)
    monkeypatch.setattr(scan, "open_camera", lambda **kwargs: object())
    monkeypatch.setattr(scan, "close_camera", lambda cam: None)
    monkeypatch.setattr(camera_executor, "open_camera", lambda **kwargs: object())
    monkeypatch.setattr(camera_executor, "close_camera", lambda cam: None)
    monkeypatch.setattr(scan, "capture_with_opened_camera", capture)
    params = {
        "task_id": "jpg-test", "task_type": "capture", "plate_type": "24-well",
        "well_name": "A1", "objective_name": "4x", "settle_s": 0,
        "scan_output_json": str(tmp_path / "A1" / "scan_result.json"),
        "save_dir": str(tmp_path / "A1" / "images"),
        "filename_pattern": "{index}.bmp", "device_index": 0, "stages": ["capture"],
    }
    names = ["center", "upper_left", "upper_right", "lower_left", "lower_right"]
    plan = {
        "points": [
            {"index": index, "point_name": name, "row_index": 0, "col_index": 0,
             "view_down_mm": 0, "view_right_mm": 0, "stage_x_target": 1, "stage_y_target": 2}
            for index, name in enumerate(names, 1)
        ],
        "reference": {}, "scan_config": {"mode": "five_point"},
    }
    return {"plate": {}, "task": {}}, params, plan, originals


def assert_copies(result, originals):
    directory = Path(result["jpg_dir"])
    assert directory.name == "images_jpg"
    assert len(list(directory.glob("*.jpg"))) == len(result["captures"])
    for item in result["captures"]:
        capture = item["capture_result"]
        source = Path(capture["saved_path"])
        preview = Path(capture["jpg_path"])
        assert preview == source.parent.parent / "images_jpg" / source.with_suffix(".jpg").name
        assert source.read_bytes() == originals[source]
        with Image.open(preview) as image:
            assert image.format == "JPEG"
            assert image.size == (137, 91)
            assert JpegImagePlugin.get_sampling(image) == 0
            assert image.quantization[0][:8] == [3, 2, 2, 3, 5, 8, 10, 12]
    assert not list(directory.glob("*.part"))


def test_single_well_five_point_jpeg_results(observation):
    ctx, params, plan, originals = observation
    params["_scan_plans_by_well"] = {"A1": plan}
    result = run_task.run_single_well_pipeline(ctx, params)
    capture = result["capture_result"]
    assert capture["image_count"] == 5
    assert_copies(capture, originals)
    assert capture == json.loads(Path(params["scan_output_json"]).read_text(encoding="utf-8"))


def test_multiple_wells_have_separate_jpeg_directories(observation, tmp_path):
    ctx, params, plan, originals = observation
    params["save_dir"] = str(tmp_path)
    params["_scan_plans_by_well"] = {"A1": plan, "A2": plan}
    result = run_task.run_well_list_pipeline(ctx, params, ["A1", "A2"])
    for well in result["wells"]:
        capture = well["result"]["capture_result"]
        assert Path(capture["jpg_dir"]) == tmp_path / well["well_name"] / "images_jpg"
        assert capture["image_count"] == 5
        assert_copies(capture, originals)


@pytest.mark.parametrize("failure", ["encode", "replace", "mkdir"])
def test_jpeg_failure_does_not_fail_capture(observation, monkeypatch, failure):
    ctx, params, plan, originals = observation

    def fail(*args, **kwargs):
        raise OSError("simulated JPEG failure")

    if failure == "encode":
        save = Image.Image.save

        def fail_jpeg(self, fp, format=None, **kwargs):
            if format == "JPEG":
                fail()
            return save(self, fp, format=format, **kwargs)

        monkeypatch.setattr(Image.Image, "save", fail_jpeg)
    elif failure == "replace":
        replace = overlay_preview.os.replace

        def fail_jpeg_replace(source, destination):
            if Path(destination).suffix == ".jpg":
                fail()
            return replace(source, destination)

        monkeypatch.setattr(overlay_preview.os, "replace", fail_jpeg_replace)
    else:
        destination = Path(params["save_dir"]).parent / "images_jpg"
        destination.parent.mkdir(parents=True)
        destination.write_text("blocked", encoding="utf-8")
    result = scan.execute_scan_capture(ctx, params, plan)
    assert result["status"] == "success"
    assert result["image_count"] == 5
    assert all(item["capture_result"]["jpg_path"] is None for item in result["captures"])
    assert all(path.read_bytes() == content for path, content in originals.items())
    assert not list(Path(params["save_dir"]).parent.rglob("*.part"))


@pytest.mark.parametrize("failure", ["cancel", "capture"])
def test_partial_scan_keeps_completed_jpeg_paths(observation, monkeypatch, failure):
    ctx, params, plan, originals = observation

    def check(params, stage):
        if stage == "before_capture:point_2":
            if failure == "cancel":
                raise TaskCanceled("canceled")
            raise RuntimeError("camera failed")

    monkeypatch.setattr(scan, "raise_if_cancel_requested", check)
    with pytest.raises(TaskCanceled if failure == "cancel" else RuntimeError):
        scan.execute_scan_capture(ctx, params, plan)
    result = json.loads(Path(params["scan_output_json"]).read_text(encoding="utf-8"))
    assert result["status"] == ("canceled" if failure == "cancel" else "failed")
    assert result["completed_image_count"] == 1
    assert_copies(result, originals)


def test_pipeline_capture_does_not_add_jpeg_copies(observation):
    ctx, params, plan, originals = observation
    params["task_type"] = "pipeline"
    result = scan.execute_scan_capture(ctx, params, plan)
    assert "jpg_dir" not in result
    assert all("jpg_path" not in item["capture_result"] for item in result["captures"])
    assert not (Path(params["save_dir"]).parent / "images_jpg").exists()


def test_preview_cannot_overwrite_source(tmp_path):
    source = tmp_path / "original.jpg"
    Image.new("L", (10, 10), 128).save(source)
    before = source.read_bytes()
    assert overlay_preview.save_jpeg_preview(source, source) is None
    assert source.read_bytes() == before
