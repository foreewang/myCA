import json
from pathlib import Path

import pytest
from PIL import Image, JpegImagePlugin

from workflow import detect_executor, overlay_preview


def test_preview_keeps_dimensions_source_and_jpeg_encoding(tmp_path):
    source = tmp_path / "彩色_overlay.bmp"
    Image.new("RGB", (137, 91), (30, 190, 70)).save(source)
    original = source.read_bytes()
    path = overlay_preview.save_overlay_preview(source)
    with Image.open(path) as preview:
        assert preview.format == "JPEG"
        assert preview.size == (137, 91)
        assert JpegImagePlugin.get_sampling(preview) == 0  # 4:4:4
        # Standard JPEG quality-90 luminance quantization table prefix.
        assert preview.quantization[0][:8] == [3, 2, 2, 3, 5, 8, 10, 12]
    assert source.read_bytes() == original
    Image.new("RGB", (137, 91), (220, 20, 20)).save(source)
    assert overlay_preview.save_overlay_preview(source) == path
    with Image.open(path) as preview:
        assert preview.getpixel((50, 50))[0] > 200
    assert not list(tmp_path.glob("*.part"))


@pytest.mark.parametrize("failure", ["encode", "replace"])
def test_failure_does_not_publish_partial_or_return_stale_preview(tmp_path, monkeypatch, failure):
    source = tmp_path / "overlay.bmp"
    Image.new("RGB", (32, 32), "red").save(source)
    path = Path(overlay_preview.save_overlay_preview(source))
    previous = path.read_bytes()
    Image.new("RGB", (32, 32), "blue").save(source)

    def fail(*args, **kwargs):
        raise OSError("simulated write failure")

    if failure == "encode":
        monkeypatch.setattr(Image.Image, "save", fail)
    else:
        monkeypatch.setattr(overlay_preview.os, "replace", fail)
    assert overlay_preview.save_overlay_preview(source) is None
    assert path.read_bytes() == previous
    assert not list(tmp_path.glob("*.part"))


@pytest.mark.parametrize("mode", ["vision", "workflow", "disabled", "failure"])
def test_workflow_and_export_publish_only_current_preview(tmp_path, monkeypatch, mode):
    source = tmp_path / "source.bmp"
    Image.new("L", (100, 80), 128).save(source)
    before = source.read_bytes()

    def detect(image_path, **kwargs):
        assert image_path == str(source)
        output = kwargs["detect_kwargs"].get("out_dir")
        if output:
            Path(output).mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (100, 80), "green").save(Path(output) / "06_overlay.bmp")
        return {"clones": []}

    monkeypatch.setattr(detect_executor, "run_detect_on_image", detect)
    if mode == "failure":
        monkeypatch.setattr(detect_executor, "save_overlay_preview", lambda p: None)
    cfg = {"entrypoint": "vision.vision.detect_pipeline:process_image",
           "save_overlay": mode != "disabled",
           "overlay_source": "workflow" if mode == "workflow" else "vision"}
    params = {"task_id": "preview", "plate_type": "test", "well_name": "A1",
              "objective_name": "4x", "detect_output_json": str(tmp_path / "detect_result.json")}
    scan = {"scan_config": {"fov_mm": {"width": 1, "height": 1}},
            "captures": [{"index": 1, "row_index": 0, "col_index": 0,
                          "stage_x_target": 0, "stage_y_target": 0,
                          "capture_result": {"saved_path": str(source)}}]}
    result = detect_executor.execute_detect_on_scan_result({"task": {"detect": cfg}}, params, scan)
    image = result["images"][0]
    preview = image["overlay_preview_path"]
    if mode in {"vision", "workflow"}:
        assert Path(preview).is_file()
        with Image.open(preview) as im:
            assert im.size == (100, 80)
            assert JpegImagePlugin.get_sampling(im) == 0
    else:
        assert preview is None
    assert (image["overlay_image_path"] is None) == (mode == "disabled")
    assert image["image_path"] == str(source)
    assert source.read_bytes() == before
    for filename in ("detect_result.json", "pickable_detect_result.json"):
        saved = json.loads((tmp_path / filename).read_text(encoding="utf-8"))
        assert saved["images"][0]["overlay_preview_path"] == preview
