"""Rule-only debug output options must preserve workflow and CLI routing."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import numpy as np
from PIL import Image

from vision import run_detect
from vision.vision import detect_pipeline, instance_pipeline
from vision.vision.model_runtime import VisionModelError
from workflow import detect_api, detect_executor


RULE_ENTRYPOINTS = (
    "vision.vision.detect_pipeline:process_image",
    "vision.detect_pipeline:process_image",
)


@pytest.mark.parametrize("option,value", [
    ("detect_well_border", True),
    ("well_border_margin_mm", 1.0),
    ("well_border_margin_px", 30.0),
])
def test_retired_well_parameters_are_not_accepted_by_vision_entrypoints(
    tmp_path: Path, option: str, value,
) -> None:
    source = np.full((40, 40), 128, np.uint8)
    image_path = tmp_path / "capture.bmp"
    Image.fromarray(source).save(image_path)
    kwargs = {option: value}
    for fn, arg in (
        (detect_pipeline.detect_and_refine, source),
        (detect_pipeline.detect_from_gray, source),
        (detect_pipeline.detect_from_path, image_path),
        (detect_pipeline.process_image, image_path),
    ):
        with pytest.raises(TypeError, match=option):
            fn(arg, **kwargs)
    with pytest.raises(VisionModelError, match=f"unsupported model pipeline arguments.*{option}"):
        instance_pipeline.detect_from_array(source, model_dir="unused", **kwargs)


@pytest.fixture
def scan_inputs(tmp_path: Path) -> tuple[dict, dict, dict]:
    image_path = tmp_path / "capture.bmp"
    Image.new("L", (40, 40), color=128).save(image_path)
    return (
        {"task": {"detect": {"entrypoint": RULE_ENTRYPOINTS[0]}}},
        {
            "task_id": "rule-output-contract",
            "plate_type": "test",
            "well_name": "A1",
            "objective_name": "4x",
        },
        {
            "scan_config": {"fov_mm": {"width": 1.0, "height": 1.0}},
            "captures": [
                {
                    "index": 1,
                    "row_index": 0,
                    "col_index": 0,
                    "stage_x_target": 0,
                    "stage_y_target": 0,
                    "capture_result": {"saved_path": str(image_path)},
                }
            ],
        },
    )


def _capture_rule_calls(monkeypatch: pytest.MonkeyPatch, entrypoint: str) -> dict:
    """Exercise the real resolver and path wrappers, replacing only inference."""
    monkeypatch.setattr(sys, "path", list(sys.path))
    fn = detect_api._resolve_callable(entrypoint)
    module = sys.modules[fn.__module__]
    captured: dict = {}
    original_run = detect_executor.run_detect_on_image

    def observe_api(*args, **kwargs):
        captured["api_kwargs"] = dict(kwargs["detect_kwargs"])
        return original_run(*args, **kwargs)

    def fake_detect_normalized_gray(gray, **kwargs):
        captured["rule_kwargs"] = kwargs
        return {"component_count": 0, "components": []}

    monkeypatch.setattr(detect_executor, "run_detect_on_image", observe_api)
    monkeypatch.setattr(module, "_detect_from_normalized_gray", fake_detect_normalized_gray)
    return captured


@pytest.mark.parametrize("entrypoint", RULE_ENTRYPOINTS)
@pytest.mark.parametrize("debug_option", [None, False, True], ids=["default", "minimal", "debug"])
def test_rule_workflow_routes_debug_option_through_real_alias(
    scan_inputs, monkeypatch: pytest.MonkeyPatch, entrypoint: str, debug_option: bool | None
) -> None:
    ctx, params, scan_result = scan_inputs
    cfg = ctx["task"]["detect"]
    cfg["entrypoint"] = entrypoint
    cfg["seed_thresh"] = 99  # Unrelated algorithm options must not gain passthrough.
    if debug_option is not None:
        cfg["save_debug"] = debug_option
    captured = _capture_rule_calls(monkeypatch, entrypoint)

    result = detect_executor.execute_detect_on_scan_result(ctx, params, scan_result)

    expected_debug = debug_option is True
    assert captured["api_kwargs"]["save_debug"] is expected_debug
    assert captured["rule_kwargs"]["save_debug"] is expected_debug
    assert captured["rule_kwargs"]["out_dir"].endswith("capture_vision")
    assert "seed_thresh" not in captured["api_kwargs"]
    assert "model_dir" in captured["api_kwargs"]
    assert "model_dir" not in captured["rule_kwargs"]
    assert result["total_clone_count"] == 0


@pytest.mark.parametrize("entrypoint", RULE_ENTRYPOINTS)
@pytest.mark.parametrize("save_overlay,overlay_source", [(False, "vision"), (True, "workflow")])
def test_debug_flag_preserves_existing_output_disable_and_workflow_overlay(
    scan_inputs, monkeypatch: pytest.MonkeyPatch, entrypoint: str,
    save_overlay: bool, overlay_source: str,
) -> None:
    ctx, params, scan_result = scan_inputs
    ctx["task"]["detect"].update(
        entrypoint=entrypoint, save_debug=True,
        save_overlay=save_overlay, overlay_source=overlay_source,
    )
    captured = _capture_rule_calls(monkeypatch, entrypoint)

    result = detect_executor.execute_detect_on_scan_result(ctx, params, scan_result)

    assert "out_dir" not in captured["api_kwargs"]
    assert "scale_bar" not in captured["api_kwargs"]
    assert captured["rule_kwargs"]["out_dir"] is None
    assert captured["rule_kwargs"]["save_debug"] is True
    overlay_path = result["images"][0]["overlay_image_path"]
    if save_overlay:
        assert Path(overlay_path).is_file()
        assert overlay_path.endswith("_detect_overlay.png")
    else:
        assert overlay_path is None
        assert result["detect_overlay_dir"] is None


@pytest.mark.parametrize("invalid", [None, 0, 1, "false", "true", [], {}])
def test_save_debug_rejects_non_boolean_before_image_processing(scan_inputs, invalid) -> None:
    ctx, params, scan_result = scan_inputs
    ctx["task"]["detect"]["save_debug"] = invalid
    scan_result["captures"][0]["capture_result"]["saved_path"] = "missing.bmp"
    with pytest.raises(ValueError, match=r"detect\.save_debug must be a boolean"):
        detect_executor.execute_detect_on_scan_result(ctx, params, scan_result)


@pytest.mark.parametrize(
    "entrypoint",
    [
        None,
        "vision.vision.instance_pipeline:process_image",
        "vision.instance_pipeline:process_image",
        "vision.vision:process_image",
        "vendor.detector:process_image",
        "vision.vision:legacy_detect_from_path",
        "vision.vision.detect_pipeline:detect_from_path",
        "vision.detect_pipeline:detect_from_path",
    ],
)
def test_model_and_other_entrypoints_receive_unchanged_kwargs(
    scan_inputs, monkeypatch: pytest.MonkeyPatch, entrypoint: str | None
) -> None:
    ctx, params, scan_result = scan_inputs
    ctx["task"]["detect"].update(entrypoint=entrypoint, save_debug=True, save_overlay=False)
    captured: dict = {}

    def fake_detect(_path, **kwargs):
        captured.update(kwargs["detect_kwargs"])
        return {"schema_version": 1, "clones": [], "review_candidates": []}

    monkeypatch.setattr(detect_executor, "run_detect_on_image", fake_detect)
    detect_executor.execute_detect_on_scan_result(ctx, params, scan_result)
    assert set(captured) == {
        "model_dir", "provider", "allow_cpu_fallback", "objective_name",
        "mm_per_pixel",
    }


@pytest.mark.parametrize("save_debug", [False, True])
def test_legacy_cli_forwards_debug_choice(monkeypatch: pytest.MonkeyPatch, capsys, save_debug: bool) -> None:
    captured: dict = {}

    def fake_detect(image_path, **kwargs):
        captured.update(image_path=image_path, **kwargs)
        return {"component_count": 0}

    monkeypatch.setattr(detect_pipeline, "detect_from_path", fake_detect)
    argv = ["run_detect.py", "sample.bmp", "--backend", "legacy", "--out-dir", "results"]
    if save_debug:
        argv.append("--save-debug")
    monkeypatch.setattr(sys, "argv", argv)
    run_detect.main()
    assert captured["save_debug"] is save_debug
    assert captured["out_dir"] == "results"
    assert '"component_count": 0' in capsys.readouterr().out


def test_model_cli_rejects_debug_flag_before_inference(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    def forbidden_detect(*args, **kwargs):
        pytest.fail("model inference must not run for a legacy-only flag")

    monkeypatch.setattr(instance_pipeline, "detect_from_path", forbidden_detect)
    monkeypatch.setattr(
        sys, "argv",
        ["run_detect.py", "sample.bmp", "--model-dir", "model-release", "--save-debug"],
    )
    with pytest.raises(SystemExit) as exc:
        run_detect.main()
    assert exc.value.code == 2
    assert "--save-debug is only supported when --backend=legacy" in capsys.readouterr().err


def test_model_cli_keeps_original_call_contract(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    captured: dict = {}

    def fake_detect(image_path, **kwargs):
        captured.update(kwargs)
        return {"component_count": 0}

    monkeypatch.setattr(instance_pipeline, "detect_from_path", fake_detect)
    monkeypatch.setattr(sys, "argv", ["run_detect.py", "sample.bmp", "--model-dir", "model-release"])
    run_detect.main()
    assert "save_debug" not in captured
    assert captured["model_dir"] == "model-release"
    assert captured["provider"] == "cuda"
    capsys.readouterr()
