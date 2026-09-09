"""执行 capture、pipeline、compensate 和 handoff 任务的主工作流。"""
from __future__ import annotations

import argparse
import copy
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

from workflow.file_io import atomic_write_json, read_json_with_retry, read_text_with_retry
from workflow.task_control import raise_if_cancel_requested, report_progress
from workflow.task_store import normalize_task_objective_alias, task_objective_name
from workflow.detect_api import rule_texture_kwargs

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from workflow.config_validator import resolve_mvs_python_dir, validate_autofocus_file
from workflow.platform_defaults import DEFAULT_SCAN_OVERLAP, DEFAULT_SCAN_SETTLE_S, apply_motion_profile_defaults


def load_structured_file(path: str | Path) -> Dict[str, Any]:
    path = Path(path)
    text = read_text_with_retry(path)
    return json.loads(text) if path.suffix.lower() == ".json" else (yaml.safe_load(text) or {})


def task_cfg_to_runtime_yaml(raw_task_cfg: Dict[str, Any]) -> Tuple[str, str]:
    tmp = tempfile.NamedTemporaryFile(
        prefix="task_",
        suffix=".yaml",
        delete=False,
        mode="w",
        encoding="utf-8",
    )
    yaml.safe_dump(raw_task_cfg, tmp, allow_unicode=True, sort_keys=False)
    tmp_path = tmp.name
    tmp.close()
    return tmp_path, tmp_path


def task_path_for_runtime_context(task_path: str | Path) -> Tuple[str, str | None]:
    task_path = Path(task_path)
    if task_path.suffix.lower() != ".json":
        return str(task_path), None

    raw = load_structured_file(task_path)
    return task_cfg_to_runtime_yaml(raw)


def save_result(result: Dict[str, Any], dump_json: str | None) -> None:
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if dump_json:
        atomic_write_json(dump_json, result)


def write_result(result: Dict[str, Any], dump_json: str | None) -> None:
    if dump_json:
        atomic_write_json(dump_json, result)




def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "y", "on", "enable", "enabled"}:
            return True
        if text in {"0", "false", "no", "n", "off", "disable", "disabled"}:
            return False
    return default


def _normalize_objective_name(value: Any) -> str:
    return str(value or "").strip().lower()


def load_local_autofocus_policy(task: Dict[str, Any], default_config_dir: Path) -> Dict[str, Any]:
    """Load local autofocus policy.

    后端不需要传 autofocus。默认读取本地 config/autofocus.yaml。
    config/autofocus.yaml 既可以被 run_autofocus 作为算法/硬件配置读取，
    也可以额外包含 enabled / trigger / config_path 等策略字段。
    如果 task.autofocus 存在，则仅作为本地调试/兼容覆盖项使用。
    """
    default_path = default_config_dir / "autofocus.yaml"
    cfg: Dict[str, Any] = {}

    if default_path.exists():
        local_cfg = validate_autofocus_file(
            default_path,
            objectives_path=default_config_dir / "objectives.yaml",
            camera_path=default_config_dir / "camera.yaml",
        )
        if isinstance(local_cfg.get("autofocus"), dict):
            cfg.update(local_cfg["autofocus"])
        else:
            for key in ("enabled", "trigger", "config_path", "force"):
                if key in local_cfg:
                    cfg[key] = local_cfg[key]

    task_cfg = task.get("autofocus")
    if isinstance(task_cfg, dict):
        cfg.update(task_cfg)

    cfg.setdefault("enabled", True)
    cfg.setdefault("config_path", str(default_path))

    trigger_cfg = cfg.get("trigger")
    if not isinstance(trigger_cfg, dict):
        trigger_cfg = {}
    trigger_cfg.setdefault("after_objective_switch", True)
    trigger_cfg.setdefault("always_before_capture", False)
    trigger_cfg.setdefault("always_before_capture_objectives", [])
    trigger_cfg.setdefault("run_at", "before_first_capture_after_stage_move")
    trigger_cfg.setdefault("scope", "once_per_well")
    cfg["trigger"] = trigger_cfg
    return cfg


def should_run_autofocus(
    *,
    task_type: str,
    task_cfg: Dict[str, Any],
    objective_result: Dict[str, Any],
    autofocus_cfg: Dict[str, Any],
) -> Tuple[bool, str]:
    """Decide whether autofocus should run before an observation task."""
    if task_type not in {"capture", "pipeline"}:
        return False, "task_type_not_observation"

    if not _as_bool(autofocus_cfg.get("enabled"), default=True):
        return False, "autofocus_disabled"

    trigger_cfg = autofocus_cfg.get("trigger", {}) or {}

    if _as_bool(autofocus_cfg.get("force"), default=False) or _as_bool(trigger_cfg.get("force"), default=False):
        return True, "force"

    if _as_bool(trigger_cfg.get("after_objective_switch"), default=True) and bool(objective_result.get("switched", False)):
        return True, "objective_switched"

    if _as_bool(trigger_cfg.get("always_before_capture"), default=False):
        return True, "always_before_capture"

    objective_name = _normalize_objective_name(task_objective_name(task_cfg))
    always_objectives = {
        _normalize_objective_name(x)
        for x in (trigger_cfg.get("always_before_capture_objectives", []) or [])
    }
    if objective_name and objective_name in always_objectives:
        return True, f"objective_policy:{objective_name}"

    return False, "no_trigger_matched"


def _default_stages(task: Dict[str, Any]) -> List[str]:
    stages = task.get("stages")
    if stages:
        return [str(x).strip().lower() for x in stages]

    task_type = str(task.get("task_type") or "").strip().lower()
    if task_type == "capture":
        return ["capture"]
    if task_type == "pipeline":
        return ["capture"]
    if task_type == "compensate":
        return []
    if task_type == "handoff":
        return []

    raise ValueError(f"无法推断 stages，请在 task 中显式提供 stages。task_type={task_type!r}")

def _camera_setting_for_objective(
    camera_cfg: Dict[str, Any],
    objective_name: str,
    key: str,
    default: Any = None,
) -> Any:
    objective_name = str(objective_name or "").strip()
    objective_settings = camera_cfg.get("objective_settings", {}) or {}

    per_objective = objective_settings.get(objective_name)
    if per_objective is None:
        per_objective = objective_settings.get(objective_name.lower())

    if isinstance(per_objective, dict) and key in per_objective:
        return per_objective[key]

    return camera_cfg.get(key, default)

def build_pipeline_params(ctx: Dict[str, Any]) -> Dict[str, Any]:
    task = ctx["task"]
    objective = ctx["objective"]
    camera = ctx["camera"]

    scan_cfg = task.get("scan", {}) or {}
    capture_cfg = task.get("capture", {}) or {}
    detect_cfg = task.get("detect", {}) or {}
    compensate_cfg = task.get("compensate", {}) or {}
    target_cfg = task.get("target", {}) or {}
    output_cfg = task.get("output", {}) or {}
    objective_name = task_objective_name(task)
    if not objective_name:
        raise KeyError("task.objective_name 不能为空")

    if scan_cfg.get("use_objective_fov", True):
        fov_w = objective["fov_mm"]["width"]
        fov_h = objective["fov_mm"]["height"]
    else:
        fov_override = scan_cfg.get("fov_override_mm")
        if isinstance(fov_override, dict):
            fov_w = fov_override["width"]
            fov_h = fov_override["height"]
        else:
            fov_w = fov_override
            fov_h = fov_override

    overlap = scan_cfg.get("overlap")
    if overlap is None:
        overlap = DEFAULT_SCAN_OVERLAP
    settle_s = scan_cfg.get("settle_s")
    if settle_s is None:
        settle_s = DEFAULT_SCAN_SETTLE_S

    return {
        "task_id": task["task_id"],
        "task_type": str(task.get("task_type") or "pipeline"),
        "stages": _default_stages(task),
        "observe_scope": task.get("observe_scope"),
        "plate_type": task["plate_type"],
        "well_name": task.get("well_name") or target_cfg.get("well_name"),
        "well_list": [str(x) for x in target_cfg.get("well_list", [])],
        "objective_name": objective_name,
        "fov_mm": {"width": fov_w, "height": fov_h},
        "resolution": camera["resolution"],
        "mvs_python_dir": resolve_mvs_python_dir(camera),
        "device_index": camera.get("device_index", 0),
        "serial_number": camera.get("serial_number"),
        "camera_ip": camera.get("ip"),
        "pixel_format": camera.get("pixel_format", "mono8"),
        "exposure_us": _camera_setting_for_objective(
            camera,
            objective_name,
            "exposure_us",
            camera.get("exposure_us"),
        ),
        "gain": _camera_setting_for_objective(
            camera,
            objective_name,
            "gain",
            camera.get("gain"),
        ),
        "save_dir": capture_cfg.get("save_dir"),
        "filename_pattern": capture_cfg.get("filename_pattern"),
        "overlap": overlap,
        "settle_s": settle_s,
        "scan_output_json": scan_cfg.get("output_json"),
        "motion": apply_motion_profile_defaults(task.get("motion")),
        "detect_entrypoint": detect_cfg.get("entrypoint"),
        "detect_rule_options": rule_texture_kwargs(detect_cfg.get("entrypoint"), detect_cfg),
        "detect_model_dir": detect_cfg.get("model_dir"),
        "detect_provider": detect_cfg.get("provider", "cuda"),
        "detect_allow_cpu_fallback": bool(detect_cfg.get("allow_cpu_fallback", False)),
        "detect_output_json": detect_cfg.get("output_json") or output_cfg.get("detect_json"),
        "scan_result_json": detect_cfg.get("input_scan_result_json") or output_cfg.get("scan_json") or scan_cfg.get("output_json"),
        "compensate_selector": compensate_cfg.get("selector", {}) or {},
        "compensate_approach": compensate_cfg.get("approach", {}) or {},
        "compensate_closed_loop": compensate_cfg.get("closed_loop", {}) or {},
        "compensate_scale": compensate_cfg.get("scale", {}) or {},
        "compensate_input_detect_json": compensate_cfg.get("input_detect_json") or output_cfg.get("detect_json"),
        "compensate_input_detect_result": compensate_cfg.get("input_detect_result"),
        "compensate_output_json": compensate_cfg.get("output_json") or output_cfg.get("compensate_json"),
        "result_output_json": output_cfg.get("result_json"),
    }


def run_single_well_capture(ctx: Dict[str, Any], params: Dict[str, Any], cam=None) -> Dict[str, Any]:
    from workflow.scan_planner import plan_single_well_scan
    from workflow.scan_executor import execute_scan_capture

    plan = plan_single_well_scan(ctx, params)
    return execute_scan_capture(ctx, params, plan, cam=cam)


def run_single_well_detect(ctx: Dict[str, Any], params: Dict[str, Any], scan_result: Dict[str, Any]) -> Dict[str, Any]:
    from workflow.detect_executor import execute_detect_on_scan_result

    return execute_detect_on_scan_result(ctx, params, scan_result)


def run_single_well_compensate(ctx: Dict[str, Any], params: Dict[str, Any], detect_result: Dict[str, Any]) -> Dict[str, Any]:
    from workflow.compensate_executor import execute_compensate_on_detect_result

    return execute_compensate_on_detect_result(ctx, params, detect_result)


def run_compensate_task(ctx: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
    detect_result = params.get("compensate_input_detect_result")
    if detect_result is None:
        detect_json = params.get("compensate_input_detect_json")
        if not detect_json:
            raise ValueError("独立 compensate 任务要求 compensate.input_detect_json 或 compensate.input_detect_result 非空")

        detect_path = Path(detect_json)
        if not detect_path.exists():
            raise FileNotFoundError(f"未找到 detect_result.json: {detect_json}")

        detect_result = read_json_with_retry(detect_path)

    if "images" not in detect_result:
        raise ValueError("输入的 detect_result 不符合单孔 detect 结果格式，缺少 images 字段")

    return run_single_well_compensate(ctx, params, detect_result)


def _default_result_paths(base_save_dir: Path, well_name: str) -> Dict[str, str]:
    well_dir = base_save_dir / well_name
    return {
        "save_dir": str(well_dir / "images"),
        "scan_output_json": str(well_dir / "scan_result.json"),
        "detect_output_json": str(well_dir / "detect_result.json"),
        "compensate_output_json": str(well_dir / "compensate_result.json"),
    }


def _set_progress_parent(params: Dict[str, Any], base: float, span: float) -> None:
    params["_progress_parent_base"] = float(base)
    params["_progress_parent_span"] = float(span)


def _set_progress_window(params: Dict[str, Any], local_base: float, local_span: float) -> None:
    parent_base = float(params.get("_progress_parent_base", 0.0) or 0.0)
    parent_span = float(params.get("_progress_parent_span", 100.0) or 100.0)
    params["_progress_base"] = parent_base + parent_span * (float(local_base) / 100.0)
    params["_progress_span"] = parent_span * (float(local_span) / 100.0)


def _derive_well_ctx_params(base_ctx: Dict[str, Any], base_params: Dict[str, Any], well_name: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    well_ctx = copy.deepcopy(base_ctx)
    well_params = copy.deepcopy(base_params)

    well_ctx["task"]["well_name"] = well_name
    if "target" in well_ctx["task"] and isinstance(well_ctx["task"]["target"], dict):
        well_ctx["task"]["target"]["well_name"] = well_name

    well_params["well_name"] = well_name
    well_params["task_id"] = f"{base_params['task_id']}_{well_name}"

    base_save_dir = Path(base_params["save_dir"])
    defaults = _default_result_paths(base_save_dir, well_name)
    well_params["save_dir"] = defaults["save_dir"]
    well_params["scan_output_json"] = defaults["scan_output_json"]
    well_params["detect_output_json"] = defaults["detect_output_json"]
    well_params["compensate_output_json"] = defaults["compensate_output_json"]
    well_params["scan_result_json"] = defaults["scan_output_json"]

    return well_ctx, well_params


def _run_single_well_pipeline(ctx: Dict[str, Any], params: Dict[str, Any], cam=None) -> Dict[str, Any]:
    stages = params["stages"]
    stage_results: Dict[str, Any] = {}
    if "_progress_parent_base" not in params:
        _set_progress_parent(params, 5.0, 90.0)

    raise_if_cancel_requested(params, "before_capture")
    if "capture" in stages:
        _set_progress_window(params, 0.0, 65.0)
        report_progress(params, "capture", 0, params.get("well_name"), "capture started")
        stage_results["capture"] = run_single_well_capture(ctx, params, cam=cam)
        report_progress(params, "capture", 100, params.get("well_name"), "capture completed")
    else:
        raise ValueError("当前 pipeline 版本要求 stages 至少包含 capture。")

    raise_if_cancel_requested(params, "after_capture")
    if "detect" in stages:
        _set_progress_window(params, 65.0, 25.0)
        report_progress(params, "detect", 0, params.get("well_name"), "detect started")
        stage_results["detect"] = run_single_well_detect(ctx, params, stage_results["capture"])
        report_progress(params, "detect", 100, params.get("well_name"), "detect completed")

    raise_if_cancel_requested(params, "after_detect")
    if "compensate" in stages:
        if "detect" not in stage_results:
            raise ValueError("compensate 依赖 detect，请在 stages 中包含 detect。")
        _set_progress_window(params, 90.0, 10.0)
        report_progress(params, "compensate", 0, params.get("well_name"), "compensate started")
        stage_results["compensate"] = run_single_well_compensate(ctx, params, stage_results["detect"])
        report_progress(params, "compensate", 100, params.get("well_name"), "compensate completed")

    return {
        "task_id": params["task_id"],
        "status": "success",
        "task_type": params["task_type"],
        "stages": params["stages"],
        "observe_scope": "single_well",
        "plate_type": params["plate_type"],
        "well_name": params["well_name"],
        "objective_name": params["objective_name"],
        "capture_result": stage_results.get("capture"),
        "detect_result": stage_results.get("detect"),
        "compensate_result": stage_results.get("compensate"),
    }


def run_single_well_pipeline(ctx: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
    if not params.get("well_name"):
        raise ValueError("single_well 流程要求 well_name 非空")
    return _run_single_well_pipeline(ctx, params)


def run_well_list_pipeline(ctx: Dict[str, Any], params: Dict[str, Any], well_list: List[str]) -> Dict[str, Any]:
    from workflow.camera_executor import open_camera, close_camera

    base_save_dir = Path(params["save_dir"])
    wells: List[Dict[str, Any]] = []

    shared_cam = None
    need_capture = "capture" in params.get("stages", [])
    need_autofocus = bool((params.get("autofocus_decision") or {}).get("should_run", False))

    # autofocus 会通过 camera_executor 获取一个受监督的短生命周期相机会话。
    # 因此需要对焦时不提前持有普通 shared_cam；后台录像会话仍可按相机身份安全复用。
    open_shared_camera = need_capture and not need_autofocus

    try:
        raise_if_cancel_requested(params, "before_open_shared_camera")
        if open_shared_camera:
            shared_cam = open_camera(
                mvs_python_dir=params.get("mvs_python_dir"),
                device_index=int(params["device_index"]),
                serial_number=params.get("serial_number"),
                camera_ip=params.get("camera_ip"),
                pixel_format=params.get("pixel_format", "mono8"),
                exposure_us=params.get("exposure_us"),
                gain=params.get("gain"),
            )

        autofocus_runtime_state: Dict[str, Any] = params.setdefault("_autofocus_runtime_state", {})

        for well_name in well_list:
            raise_if_cancel_requested(params, f"before_well:{well_name}")
            well_ctx, well_params = _derive_well_ctx_params(ctx, params, well_name)
            # deepcopy 会复制运行时状态；这里显式指回同一个对象，保证 scope=once_per_task 能跨孔生效。
            well_params["_autofocus_runtime_state"] = autofocus_runtime_state
            well_params["_cancel_check"] = params.get("_cancel_check")
            well_params["_progress_callback"] = params.get("_progress_callback")
            well_index = len(wells)
            total_wells = max(1, len(well_list))
            _set_progress_parent(well_params, 5.0 + 90.0 * well_index / total_wells, 90.0 / total_wells)
            report_progress(well_params, "well", 0, well_name, f"well {well_name} started")
            well_result = _run_single_well_pipeline(well_ctx, well_params, cam=shared_cam)
            report_progress(well_params, "well", 100, well_name, f"well {well_name} completed")
            wells.append(
                {
                    "well_name": well_name,
                    "capture_result_json": well_params["scan_output_json"],
                    "detect_result_json": well_params.get("detect_output_json"),
                    "compensate_result_json": well_params.get("compensate_output_json"),
                    "result": well_result,
                }
            )

    finally:
        if shared_cam is not None:
            close_camera(shared_cam)

    return {
        "task_id": params["task_id"],
        "status": "success",
        "task_type": params["task_type"],
        "stages": params["stages"],
        "observe_scope": "well_list",
        "plate_type": params["plate_type"],
        "objective_name": params["objective_name"],
        "base_save_dir": str(base_save_dir),
        "well_count": len(wells),
        "wells": wells,
    }


def run_full_plate_pipeline(ctx: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
    from workflow.plate_geometry import all_well_names

    wells = all_well_names(ctx["plate"])
    result = run_well_list_pipeline(ctx, params, wells)
    result["observe_scope"] = "full_plate"
    return result


def run_pipeline_task(ctx: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
    observe_scope = str(params.get("observe_scope") or "").lower()

    if observe_scope == "single_well":
        return run_single_well_pipeline(ctx, params)

    if observe_scope == "well_list":
        well_list = params.get("well_list") or []
        if not well_list:
            raise ValueError("observe_scope=well_list 时，target.well_list 不能为空")
        return run_well_list_pipeline(ctx, params, well_list)

    if observe_scope == "full_plate":
        return run_full_plate_pipeline(ctx, params)

    raise ValueError(f"不支持的 observe_scope: {observe_scope}")


def _preflight_overlap_deduplication(detect_cfg: Dict[str, Any], params: Dict[str, Any]) -> None:
    """Reject uncalibrated overlapping scans before capture or inference."""
    if float(params.get("overlap") or 0.0) > 0.0 and not bool(
        (detect_cfg.get("deduplication") or {}).get("calibrated", False)
    ):
        raise ValueError(
            "重叠视野唯一计数要求 detect.deduplication.calibrated=true；"
            "请先用带跨视野实例身份标注的数据标定 registration_tolerance_mm"
        )
    dedupe_cfg = detect_cfg.get("deduplication") or {}
    if bool(dedupe_cfg.get("calibrated", False)) and "registration_tolerance_mm" not in dedupe_cfg:
        raise ValueError(
            "detect.deduplication.calibrated=true 时必须显式配置 registration_tolerance_mm"
        )


def preflight_detection_backend(ctx: Dict[str, Any], params: Dict[str, Any]) -> None:
    """Validate/load the model before capture moves or camera acquisition begin."""
    if "detect" not in (params.get("stages") or []):
        return
    detect_cfg = (ctx.get("task") or {}).get("detect") or {}
    # Overlap unique-count is backend-agnostic: rule and third-party detectors
    # still merge physical observations after inference.
    _preflight_overlap_deduplication(detect_cfg, params)
    entrypoint = str(detect_cfg.get("entrypoint") or "").strip()
    builtin_model_entrypoints = {
        "vision.vision.instance_pipeline:process_image",
        "vision.instance_pipeline:process_image",
    }
    if entrypoint and entrypoint not in builtin_model_entrypoints:
        # Explicit legacy or third-party entrypoints own their own model
        # contract; do not force the built-in ONNX package onto them.
        return
    if str(params.get("objective_name") or "").strip().lower() != "4x":
        raise ValueError("模型定位后端只允许 4x 物镜")
    camera_resolution = (ctx.get("camera") or {}).get("resolution") or {}
    if bool(camera_resolution.get("allow_downscale", False)):
        raise ValueError("4x 模型检测要求 camera.resolution.allow_downscale=false")
    if (
        int(camera_resolution.get("width") or 0),
        int(camera_resolution.get("height") or 0),
    ) != (5120, 5120):
        raise ValueError("4x 模型检测要求相机分辨率固定为 5120x5120")
    model_dir = detect_cfg.get("model_dir") or params.get("detect_model_dir")
    if not model_dir:
        raise ValueError("detect.model_dir 是 4x 模型检测的必填项")
    from vision.vision.instance_pipeline import _cached_bundle

    _cached_bundle(
        str(Path(model_dir).resolve(strict=False)),
        str(detect_cfg.get("provider") or params.get("detect_provider") or "cuda"),
        bool(detect_cfg.get("allow_cpu_fallback", params.get("detect_allow_cpu_fallback", False))),
    )


def run_handoff_task(
    raw_task_cfg: Dict[str, Any],
    handoff_path: str | None = None,
    plates_path: str | None = None,
) -> Dict[str, Any]:
    from workflow.handoff_executor import execute_handoff_task
    from workflow.config_validator import validate_handoff_file, validate_plates_file

    default_config_dir = PROJECT_ROOT / "config"
    handoff_path = handoff_path or str(default_config_dir / "handoff.yaml")
    plates_path = plates_path or str(default_config_dir / "plates.yaml")
    handoff_root_cfg = validate_handoff_file(handoff_path)
    plates_root_cfg = validate_plates_file(plates_path)
    task = raw_task_cfg["task"]
    plate_type = str(task.get("plate_type") or "")
    plate_cfg = (plates_root_cfg.get("plates") or {}).get(plate_type)
    if not isinstance(plate_cfg, dict):
        raise KeyError(f"plates.yaml 中不存在板型: {plate_type}")
    return execute_handoff_task(task, handoff_root_cfg, plate_cfg=plate_cfg)


def execute_task_request(
    raw_task_cfg: Dict[str, Any],
    *,
    camera_path: str | None = None,
    objectives_path: str | None = None,
    plates_path: str | None = None,
    handoff_path: str | None = None,
    dump_json: str | None = None,
    persist_result: bool = True,
    cancel_check: Callable[[], bool] | None = None,
    progress_callback: Callable[[str, int | float, str | None, str], None] | None = None,
) -> Dict[str, Any]:
    if "task" not in raw_task_cfg:
        raise KeyError("task 文件缺少顶层字段 'task'")

    raw_task_cfg = copy.deepcopy(raw_task_cfg)
    task = normalize_task_objective_alias(raw_task_cfg["task"])
    raw_task_cfg["task"] = task
    task_type = str(task.get("task_type") or "").strip().lower()
    if task_type not in {"capture", "pipeline", "compensate", "handoff"}:
        raise ValueError("当前版本要求 task_type 为 capture / pipeline / compensate / handoff")

    cancel_params = {"_cancel_check": cancel_check, "_progress_callback": progress_callback}
    report_progress(cancel_params, "task", 1, None, "task started")
    raise_if_cancel_requested(cancel_params, "before_task")

    if task_type == "handoff":
        raise_if_cancel_requested(cancel_params, "before_handoff")
        report_progress(cancel_params, "handoff", 5, None, "handoff started")
        result = run_handoff_task(
            raw_task_cfg,
            handoff_path=handoff_path,
            plates_path=plates_path,
        )
        report_progress(cancel_params, "handoff", 95, None, "handoff completed")
        output_path = dump_json or ((task.get("output", {}) or {}).get("result_json"))
        if persist_result:
            write_result(result, output_path)
        return result

    from workflow.config_loader import load_runtime_context
    from workflow.objective_executor import ensure_objective_for_task, attach_objective_result

    runtime_task_path, tmp_task_path = task_cfg_to_runtime_yaml(raw_task_cfg)

    default_config_dir = PROJECT_ROOT / "config"
    camera_path = camera_path or str(default_config_dir / "camera.yaml")
    objectives_path = objectives_path or str(default_config_dir / "objectives.yaml")
    plates_path = plates_path or str(default_config_dir / "plates.yaml")

    try:
        ctx = load_runtime_context(
            task_path=runtime_task_path,
            camera_path=camera_path,
            objectives_path=objectives_path,
            plates_path=plates_path,
        )
    finally:
        if tmp_task_path:
            try:
                Path(tmp_task_path).unlink(missing_ok=True)
            except Exception:
                pass

    objectives_root_cfg = load_structured_file(objectives_path)
    ctx["objectives_cfg"] = objectives_root_cfg

    params = build_pipeline_params(ctx)
    params["_cancel_check"] = cancel_check
    params["_progress_callback"] = progress_callback
    # Validate model files/provider before objective, autofocus, stage, or camera
    # hardware is moved.
    preflight_detection_backend(ctx, params)

    raise_if_cancel_requested(cancel_params, "before_objective")
    objective_result = ensure_objective_for_task(
        task_cfg=task,
        objectives_root_cfg=objectives_root_cfg,
        extra_context={"task_id": task.get("task_id")},
    )
    raise_if_cancel_requested(cancel_params, "after_objective")

    autofocus_cfg = load_local_autofocus_policy(task, default_config_dir)
    autofocus_should_run, autofocus_reason = should_run_autofocus(
        task_type=task_type,
        task_cfg=task,
        objective_result=objective_result,
        autofocus_cfg=autofocus_cfg,
    )
    autofocus_decision = {
        "enabled": _as_bool(autofocus_cfg.get("enabled"), default=True),
        "should_run": bool(autofocus_should_run),
        "reason": autofocus_reason,
        "config_path": str(autofocus_cfg.get("config_path") or ""),
        "trigger": autofocus_cfg.get("trigger", {}) or {},
        "objective_switched": bool(objective_result.get("switched", False)),
        "run_at": "before_first_capture_after_stage_move",
    }

    # 注意：这里不再直接执行图像驱动 autofocus。
    # run_task 只完成物镜切换、预设焦点移动和 autofocus 决策。
    # 真正 autofocus 放到 scan_executor：移动到第一个扫描点并稳定后、第一张拍照前执行。
    params["objective_result"] = objective_result
    params["autofocus_cfg"] = autofocus_cfg
    params["autofocus_decision"] = autofocus_decision
    report_progress(params, "objective", 5, params.get("well_name"), "objective ready")

    if task_type == "compensate":
        raise_if_cancel_requested(params, "before_compensate")
        _set_progress_parent(params, 5.0, 90.0)
        _set_progress_window(params, 0.0, 100.0)
        report_progress(params, "compensate", 0, params.get("well_name"), "compensate started")
        result = run_compensate_task(ctx, params)
        report_progress(params, "compensate", 100, params.get("well_name"), "compensate completed")
    else:
        raise_if_cancel_requested(params, "before_pipeline")
        if str(params.get("observe_scope") or "").lower() == "single_well":
            _set_progress_parent(params, 5.0, 90.0)
        result = run_pipeline_task(ctx, params)

    result = attach_objective_result(result, objective_result)
    result["autofocus_decision"] = autofocus_decision

    output_path = dump_json or params.get("result_output_json") or params.get("compensate_output_json")
    if persist_result:
        write_result(result, output_path)

    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Run capture/detect/compensate/handoff workflow task")
    parser.add_argument("--task", required=True, help="task json/yaml path")
    parser.add_argument("--camera", default=None)
    parser.add_argument("--objectives", default=None)
    parser.add_argument("--plates", default=None)
    parser.add_argument("--handoff", default=None)
    parser.add_argument("--dump-json", default=None, help="output result json path")
    args = parser.parse_args()

    raw_task_cfg = load_structured_file(args.task)
    result = execute_task_request(
        raw_task_cfg,
        camera_path=args.camera,
        objectives_path=args.objectives,
        plates_path=args.plates,
        handoff_path=args.handoff,
        dump_json=args.dump_json,
        persist_result=True,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
