"""Five-point workflow contracts, using mocked hardware only."""
from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from workflow import run_task


ROOT = Path(__file__).resolve().parents[1]
POINT_NAMES = ["center", "upper_left", "upper_right", "lower_left", "lower_right"]


@pytest.mark.parametrize("plate_type", ["24-well", "48-well"])
@pytest.mark.parametrize("config", [None, {"enabled": False}, {"calibrated": True}])
def test_default_per_image_pipeline_exports_selectable_candidates(tmp_path, monkeypatch, plate_type, config):
    import json
    from workflow import detect_executor
    from workflow.compensate_executor import select_clone_for_compensation, _calc_compensate_target
    ctx = _context(tmp_path, plate_type=plate_type, task_type="pipeline")
    detect = {"entrypoint": "custom_detector:process_image", "save_overlay": False}
    if config is not None:
        detect["deduplication"] = config
    ctx["task"].update(stages=["capture", "detect"], detect=detect)
    events = []
    _mock_request_environment(monkeypatch, tmp_path, ctx, events)
    monkeypatch.setattr(detect_executor, "_image_size", lambda path: (100, 100))
    monkeypatch.setattr(detect_executor, "dedupe_well_clones", lambda *a, **k: pytest.fail("merging disabled"))
    monkeypatch.setattr(detect_executor, "run_detect_on_image", lambda *a, **k: {"clones": [
        {"clone_id": "C01", "bbox": [40, 40, 20, 20], "center_px": [55, 45], "is_pickable": True},
        {"clone_id": "C02", "bbox": [10, 10, 10, 10], "center_px": [15, 15], "is_pickable": False},
    ]})
    result = run_task.execute_task_request({"task": ctx["task"]}, persist_result=False)
    detected = result["detect_result"]
    assert detected["total_image_clone_count"] == 10
    assert detected["unique_clone_count"] is None and detected["total_clone_count"] is None
    assert detected["unique_clones"] is None
    exported = json.loads(Path(detected["pickable_result_json"]).read_text(encoding="utf-8"))
    assert exported["schema_version"] == 4
    assert exported["deduplication"] == {"enabled": False, "status": "not_performed"}
    assert exported["image_count"] == 5 and exported["candidate_count"] == exported["total_clone_count"] == 5
    assert [i["point_name"] for i in exported["images"]] == POINT_NAMES
    for source in detected["images"]:
        image, clone = select_clone_for_compensation(exported, {"mode": "image_and_clone", "image_index": source["index"], "clone_id": "C01"})
        assert clone["global_clone_id"] is None
        assert clone["center_px"] == source["clones"][0]["center_px"]
        assert _calc_compensate_target(ctx=ctx, params={}, image_item=image, clone_item=clone) == _calc_compensate_target(ctx=ctx, params={}, image_item=source, clone_item=source["clones"][0])


@pytest.mark.parametrize("value", [None, 0, 1, "false", "true", [], {}])
def test_enabled_requires_json_boolean(value):
    with pytest.raises(ValueError, match="enabled"):
        run_task._preflight_overlap_deduplication({"deduplication": {"enabled": value}}, {"has_overlapping_views": True})


def _context(tmp_path, *, objective="4x", plate_type="24-well", task_type="capture"):
    plates = yaml.safe_load((ROOT / "config/plates.yaml").read_text(encoding="utf-8"))["plates"]
    plate = copy.deepcopy(plates[plate_type])
    plate["a1_start"].update(
        reference_objective="4x", reference_fov_mm={"width": 3.22, "height": 3.22}
    )
    plate["five_point"] = {"radial_fraction": 0.5, "edge_margin_mm": 0.5}
    fov = 3.22 if objective == "4x" else 1.31
    return {
        "task": {
            "task_id": "five-point-workflow",
            "task_type": task_type,
            "plate_type": plate_type,
            "objective_name": objective,
            "observe_scope": "single_well",
            "target": {"well_name": "A1"},
            "capture": {
                "save_dir": str(tmp_path),
                "filename_pattern": "{well}_{index:03d}_{point_name}_r{row}_c{col}.bmp",
            },
            "motion": {"stop_shoot": {"enabled": False}},
        },
        "plate": plate,
        "objective": {"fov_mm": {"width": fov, "height": fov}},
        "camera": {"resolution": {"width": 5120, "height": 5120, "allow_downscale": False}},
    }


def _mock_request_environment(monkeypatch, tmp_path, ctx, events):
    from workflow import config_loader, objective_executor, scan_executor

    monkeypatch.setattr(run_task, "task_cfg_to_runtime_yaml", lambda raw: (tmp_path / "task.yaml", None))
    monkeypatch.setattr(config_loader, "load_runtime_context", lambda **kwargs: ctx)
    monkeypatch.setattr(run_task, "load_structured_file", lambda path: {})

    def objective(**kwargs):
        events.append(("objective",))
        return {"switched": True}

    monkeypatch.setattr(objective_executor, "ensure_objective_for_task", objective)
    monkeypatch.setattr(
        run_task,
        "load_local_autofocus_policy",
        lambda task, directory: {
            "enabled": True,
            "trigger": {"after_objective_switch": True, "scope": "once_per_well"},
        },
    )

    def move(**kwargs):
        x, y = kwargs["x_target"], kwargs["y_target"]
        events.append(("move", x, y))
        return {
            "target": {"x": x, "y": y},
            "before": {"x": {"current_pos": x}, "y": {"current_pos": y}},
            "after": {"x": {"current_pos": x}, "y": {"current_pos": y}},
            "err_to_target": {"x": 0, "y": 0},
        }

    def autofocus(**kwargs):
        point = kwargs["point"]
        events.append(("autofocus", point["point_name"]))
        return {"status": "success", "scan_point_index": point["index"]}

    def capture(**kwargs):
        fields = kwargs["format_kwargs"]
        events.append(("capture", fields["point_name"]))
        filename = kwargs["filename_pattern"].format(**fields)
        return {"saved_path": str(Path(kwargs["save_dir"]) / filename)}

    monkeypatch.setattr(scan_executor, "move_to_absolute", move)
    monkeypatch.setattr(scan_executor, "_execute_autofocus_before_capture", autofocus)
    monkeypatch.setattr(scan_executor, "open_camera", lambda **kwargs: object())
    monkeypatch.setattr(scan_executor, "close_camera", lambda camera: None)
    monkeypatch.setattr(scan_executor, "capture_with_opened_camera", capture)


@pytest.mark.parametrize("task_type", ["capture", "pipeline"])
def test_request_captures_exactly_five_points_center_first_with_center_autofocus(
    tmp_path, monkeypatch, task_type
):
    from workflow import scan_planner

    ctx = _context(tmp_path, task_type=task_type)
    events = []
    _mock_request_environment(monkeypatch, tmp_path, ctx, events)
    real_plan = scan_planner.plan_single_well_scan
    plans = []

    def plan_once(context, params):
        assert not events, "planning must finish before objective or stage hardware"
        result = real_plan(context, params)
        plans.append(result)
        return result

    monkeypatch.setattr(scan_planner, "plan_single_well_scan", plan_once)
    result = run_task.execute_task_request({"task": ctx["task"]}, persist_result=False)
    scan = result["capture_result"]
    assert len(plans) == 1, "execution must reuse the prechecked plan"
    assert scan["image_count"] == 5
    assert [item["point_name"] for item in scan["captures"]] == POINT_NAMES
    assert [event[1] for event in events if event[0] == "capture"] == POINT_NAMES
    moves = [event for event in events if event[0] == "move"]
    assert len(moves) == 5
    first = plans[0]["points"][0]
    assert moves[0][1:] == (first["stage_x_target"], first["stage_y_target"])
    assert moves[0][1:] != (ctx["plate"]["a1_start"]["x"], ctx["plate"]["a1_start"]["y"])
    assert [event for event in events if event[0] == "autofocus"] == [("autofocus", "center")]
    assert [event[0] for event in events[:4]] == ["objective", "move", "autofocus", "capture"]
    assert Path(scan["captures"][0]["capture_result"]["saved_path"]).name == "A1_001_center_r1_c1.bmp"


@pytest.mark.parametrize("scope", ["well_list", "full_plate"])
def test_later_well_outside_limits_aborts_before_any_hardware(tmp_path, monkeypatch, scope):
    ctx = _context(tmp_path)
    ctx["task"].update(observe_scope=scope, target={"well_list": ["A1", "A6"]})
    ctx["plate"]["stage_limits"]["x_min"] = 0
    events = []
    _mock_request_environment(monkeypatch, tmp_path, ctx, events)
    monkeypatch.setattr(run_task, "preflight_detection_backend", lambda *args: events.append(("detect_preflight",)))
    with pytest.raises(ValueError):
        run_task.execute_task_request({"task": ctx["task"]}, persist_result=False)
    assert events == []


@pytest.mark.parametrize("plate_type", ["24-well", "48-well"])
def test_actual_five_point_overlap_requires_calibration_before_hardware(
    tmp_path, monkeypatch, plate_type
):
    ctx = _context(tmp_path, plate_type=plate_type, task_type="pipeline")
    ctx["task"].update(stages=["capture", "detect"], scan={"overlap": 0, "has_overlapping_views": False}, detect={"deduplication": {"enabled": True}})
    events = []
    _mock_request_environment(monkeypatch, tmp_path, ctx, events)
    with pytest.raises(ValueError, match="deduplication.calibrated"):
        run_task.execute_task_request({"task": ctx["task"]}, persist_result=False)
    assert events == []


def test_calibrated_five_point_detect_preserves_all_point_names(tmp_path, monkeypatch):
    from workflow import detect_executor

    ctx = _context(tmp_path, task_type="pipeline")
    ctx["task"].update(
        stages=["capture", "detect"],
        detect={
            "entrypoint": "custom_detector:process_image",
            "save_overlay": False,
            "deduplication": {"enabled": True, "calibrated": True, "registration_tolerance_mm": 0.10},
        },
    )
    events = []
    _mock_request_environment(monkeypatch, tmp_path, ctx, events)
    monkeypatch.setattr(detect_executor, "_image_size", lambda path: (100, 100))
    monkeypatch.setattr(detect_executor, "run_detect_on_image", lambda *args, **kwargs: {"clones": []})
    result = run_task.execute_task_request({"task": ctx["task"]}, persist_result=False)
    detected = result["detect_result"]
    assert [item["point_name"] for item in detected["images"]] == POINT_NAMES
    assert detected["scan_config"]["has_overlapping_views"] is True
    assert detected["image_count"] == 5


def test_10x_defaults_to_full_coverage_and_rejects_explicit_five_point(tmp_path):
    ctx = _context(tmp_path, objective="10x")
    assert run_task.build_pipeline_params(ctx)["scan_mode"] == "full_coverage"
    ctx["task"]["scan"] = {"mode": "five_point"}
    with pytest.raises(ValueError):
        run_task.build_pipeline_params(ctx)


def test_explicit_full_coverage_and_independent_compensation_keep_old_scan_behavior(tmp_path):
    ctx = _context(tmp_path)
    ctx["task"]["scan"] = {"mode": "full_coverage"}
    params = run_task.build_pipeline_params(ctx)
    assert params["scan_mode"] == "full_coverage"
    run_task.preflight_five_point_scans(ctx, params)
    assert "_scan_plans_by_well" not in params
    ctx["task"].update(task_type="compensate", objective_name="10x", scan={"mode": "five_point"})
    assert run_task.build_pipeline_params(ctx)["scan_mode"] == "full_coverage"


def test_legacy_filename_pattern_still_works_for_five_point(tmp_path):
    from workflow.scan_executor import _format_kwargs

    ctx = _context(tmp_path)
    params = run_task.build_pipeline_params(ctx)
    run_task.preflight_five_point_scans(ctx, params)
    point = params["_scan_plans_by_well"]["A1"]["points"][0]
    assert "{well}_{index:03d}_row{row:02d}_col{col:02d}.bmp".format(
        **_format_kwargs(params, point)
    ) == "A1_001_row01_col01.bmp"


def _duplicate_images():
    images = []
    for index, stage_x, center_x in [(1, 0, 60), (2, 20, 80)]:
        images.append({
            "index": index,
            "stage_x_actual": stage_x,
            "stage_y_actual": 0,
            "image_width_px": 100,
            "image_height_px": 100,
            "image_center_px": [50, 50],
            "mm_per_pixel": {"x": 0.01, "y": 0.01},
            "clones": [{
                "clone_id": "C1",
                "offset_from_image_center_px": [center_x - 50, 0],
                "bbox": [center_x - 20, 30, 40, 40],
                "confidence": 0.9,
            }],
        })
    return images


def test_dedupe_uses_geometric_overlap_flag_even_with_zero_grid_overlap():
    from workflow.clone_dedupe import CloneDedupeError, dedupe_well_clones

    kwargs = {
        "plate_cfg": {"pulses_per_mm": 100, "x_stage_sign_for_view_right": 1, "y_stage_sign_for_view_down": 1},
        "reference": {"well_start": {"x": 0, "y": 0}},
        "scan_config": {"overlap": 0, "has_overlapping_views": True},
    }
    with pytest.raises(CloneDedupeError, match="calibrated"):
        dedupe_well_clones(_duplicate_images(), **kwargs)
    kwargs["scan_config"]["dedupe_registration_calibrated"] = True
    images = _duplicate_images()
    result = dedupe_well_clones(images, **kwargs)
    assert len(result["unique_clones"]) == 1
    assert result["unique_clones"][0]["observation_count"] == 2
    assert len(images) == 2


def test_old_dedupe_result_without_overlap_flag_keeps_legacy_guard():
    with pytest.raises(ValueError, match="deduplication.calibrated"):
        run_task._preflight_overlap_deduplication({"deduplication": {"enabled": True}}, {"overlap": 0.1})
    run_task._preflight_overlap_deduplication({"deduplication": {"enabled": True}}, {"overlap": 0})


@pytest.mark.parametrize("tolerance", [None, True, 0, -0.1, float("nan"), float("inf"), "0.1"])
def test_invalid_registration_tolerance_rejected_before_hardware(tmp_path, monkeypatch, tolerance):
    ctx = _context(tmp_path, task_type="pipeline")
    ctx["task"].update(stages=["capture", "detect"], detect={
        "entrypoint": "custom_detector:process_image",
        "deduplication": {"enabled": True, "calibrated": True, "registration_tolerance_mm": tolerance},
    })
    events = []
    _mock_request_environment(monkeypatch, tmp_path, ctx, events)
    with pytest.raises(ValueError, match="registration_tolerance_mm"):
        run_task.execute_task_request({"task": ctx["task"]}, persist_result=False)
    assert events == []


@pytest.mark.parametrize("should_focus", [False, True])
def test_successful_multiwell_captures_use_prechecked_well_ids_and_focus_policy(
    tmp_path, monkeypatch, should_focus
):
    ctx = _context(tmp_path)
    ctx["task"].update(observe_scope="well_list", target={"well_list": ["A1", "B2"]})
    events = []
    _mock_request_environment(monkeypatch, tmp_path, ctx, events)
    from workflow import camera_executor, scan_planner
    monkeypatch.setattr(camera_executor, "open_camera", lambda **kwargs: object())
    monkeypatch.setattr(camera_executor, "close_camera", lambda camera: None)
    if not should_focus:
        monkeypatch.setattr(run_task, "load_local_autofocus_policy", lambda *args: {"enabled": False})
    real_plan = scan_planner.plan_single_well_scan
    planned = []

    def precheck(context, params):
        assert events == [], "all wells must be planned before objective or XY motion"
        planned.append(params["well_name"])
        return real_plan(context, params)

    monkeypatch.setattr(scan_planner, "plan_single_well_scan", precheck)
    result = run_task.execute_task_request({"task": ctx["task"]}, persist_result=False)
    assert planned == ["A1", "B2"]
    assert result["well_count"] == 2
    for well in result["wells"]:
        name = well["well_name"]
        scan = well["result"]["capture_result"]
        assert scan["well_name"] == name
        assert scan["task_id"] == f"five-point-workflow_{name}"
        assert [c["point_name"] for c in scan["captures"]] == POINT_NAMES
        assert all(Path(c["capture_result"]["saved_path"]).name.startswith(name) for c in scan["captures"])
    assert len([e for e in events if e[0] == "move"]) == 10
    assert [e for e in events if e[0] == "autofocus"] == ([("autofocus", "center")] * 2 if should_focus else [])


def test_five_image_detection_merges_shared_targets_without_merging_neighbors(tmp_path, monkeypatch):
    from workflow import detect_executor, scan_planner

    ctx = _context(tmp_path, task_type="pipeline")
    ctx["task"].update(stages=["capture", "detect"], detect={
        "entrypoint": "custom_detector:process_image", "save_overlay": False,
        "deduplication": {"enabled": True, "calibrated": True, "registration_tolerance_mm": 0.1},
    })
    plan = scan_planner.plan_single_well_scan(ctx, run_task.build_pipeline_params(ctx))
    center = plan["reference"]["well_center"]
    # Both distinct objects lie in the center/upper-left field intersection.
    # Pixel positions follow the existing mirrored image-to-stage convention.
    objects = [(-1.35, -1.35), (-1.20, -1.35)]
    ppm = ctx["plate"]["pulses_per_mm"]
    pixel_mm = 3.22 / 5120

    def detector(image_path, **kwargs):
        point = next(p for p in plan["points"] if f"_{p['index']:03d}_" in Path(image_path).name)
        clones = []
        for index, (dx, dy) in enumerate(objects, 1):
            object_x = center["x"] - dx * ppm["x"]
            object_y = center["y"] - dy * ppm["y"]
            x = 2560 + (object_x - point["stage_x_target"]) / (pixel_mm * ppm["x"])
            y = 2560 + (object_y - point["stage_y_target"]) / (pixel_mm * ppm["y"])
            half_box = 0.04 / pixel_mm
            if half_box <= x <= 5120 - half_box and half_box <= y <= 5120 - half_box:
                clones.append({"clone_id": f"C{index}", "center_px": [x, y],
                               "bbox": [x - half_box, y - half_box, 2 * half_box, 2 * half_box],
                               "is_pickable": True, "confidence": 0.9})
        return {"schema_version": 2, "clones": clones}

    events = []
    _mock_request_environment(monkeypatch, tmp_path, ctx, events)
    monkeypatch.setattr(detect_executor, "_image_size", lambda path: (5120, 5120))
    monkeypatch.setattr(detect_executor, "run_detect_on_image", detector)
    result = run_task.execute_task_request({"task": ctx["task"]}, persist_result=False)["detect_result"]
    assert [image["point_name"] for image in result["images"]] == POINT_NAMES
    assert [len(image["clones"]) for image in result["images"]] == [2, 2, 0, 0, 0]
    assert result["total_image_clone_count"] == 4
    assert result["unique_clone_count"] == result["total_clone_count"] == 2
    assert [clone["observation_count"] for clone in result["unique_clones"]] == [2, 2]
