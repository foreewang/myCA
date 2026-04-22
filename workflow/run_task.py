from __future__ import annotations

import argparse
import copy
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Tuple

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def load_structured_file(path: str | Path) -> Dict[str, Any]:
    """读取 JSON/YAML 结构化文件。"""
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    return json.loads(text) if path.suffix.lower() == ".json" else (yaml.safe_load(text) or {})



def task_path_for_runtime_context(task_path: str | Path) -> Tuple[str, str | None]:
    """
    为 load_runtime_context 提供任务文件路径。

    当前 workflow.config_loader 以 YAML 读取为主，这里兼容 JSON 任务：
    - 若输入是 YAML，直接返回原路径
    - 若输入是 JSON，先临时转换成 YAML，再把临时路径传给下游
    """
    task_path = Path(task_path)
    if task_path.suffix.lower() != ".json":
        return str(task_path), None

    raw = load_structured_file(task_path)
    tmp = tempfile.NamedTemporaryFile(
        prefix="task_",
        suffix=".yaml",
        delete=False,
        mode="w",
        encoding="utf-8",
    )
    yaml.safe_dump(raw, tmp, allow_unicode=True, sort_keys=False)
    tmp_path = tmp.name
    tmp.close()
    return tmp_path, tmp_path



def save_result(result: Dict[str, Any], dump_json: str | None) -> None:
    """打印结果，并按需落盘。"""
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if dump_json:
        out_path = Path(dump_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")



def _default_stages(task: Dict[str, Any]) -> List[str]:
    """
    计算默认 stages。

    兼容策略：
    - task_type == 'capture' -> ['capture']
    - task_type == 'pipeline' 且未提供 stages -> ['capture']
    - 显式提供 stages 时，以 stages 为准
    """
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

    raise ValueError(f"无法推断 stages，请在 task 中显式提供 stages。task_type={task_type!r}")



def build_pipeline_params(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """把 task/camera/objective 配置整理成 pipeline 执行参数。"""
    task = ctx["task"]
    objective = ctx["objective"]
    camera = ctx["camera"]

    scan_cfg = task.get("scan", {}) or {}
    capture_cfg = task.get("capture", {}) or {}
    detect_cfg = task.get("detect", {}) or {}
    compensate_cfg = task.get("compensate", {}) or {}
    target_cfg = task.get("target", {}) or {}
    output_cfg = task.get("output", {}) or {}

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

    return {
        "task_id": task["task_id"],
        "task_type": str(task.get("task_type") or "pipeline"),
        "stages": _default_stages(task),
        "observe_scope": task.get("observe_scope"),
        "plate_type": task["plate_type"],
        "well_name": task.get("well_name") or target_cfg.get("well_name"),
        "well_list": [str(x) for x in target_cfg.get("well_list", [])],
        "objective_name": task["objective"],
        "fov_mm": {"width": fov_w, "height": fov_h},
        "resolution": camera["resolution"],
        "mvs_python_dir": camera.get("mvs_python_dir"),
        "device_index": camera.get("device_index", 0),
        "serial_number": camera.get("serial_number"),
        "exposure_us": camera.get("exposure_us"),
        "gain": camera.get("gain"),
        "save_dir": capture_cfg.get("save_dir"),
        "filename_pattern": capture_cfg.get("filename_pattern"),
        "overlap": scan_cfg.get("overlap"),
        "settle_s": scan_cfg.get("settle_s", 0.8),
        "scan_output_json": scan_cfg.get("output_json"),
        "motion": task.get("motion", {}) or {},
        "detect_entrypoint": detect_cfg.get("entrypoint"),
        "detect_output_json": detect_cfg.get("output_json") or output_cfg.get("detect_json"),
        "scan_result_json": detect_cfg.get("input_scan_result_json") or output_cfg.get("scan_json") or scan_cfg.get("output_json"),
        "compensate_selector": compensate_cfg.get("selector", {}) or {},
        "compensate_input_detect_json": (
            compensate_cfg.get("input_detect_json")
            or output_cfg.get("detect_json")
        ),
        "compensate_output_json": (
            compensate_cfg.get("output_json")
            or output_cfg.get("compensate_json")
        ),
        "result_output_json": output_cfg.get("result_json"),
    }



def run_single_well_capture(ctx: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
    from workflow.scan_planner import plan_single_well_scan
    from workflow.scan_executor import execute_scan_capture

    plan = plan_single_well_scan(ctx, params)
    return execute_scan_capture(ctx, params, plan)



def run_single_well_detect(ctx: Dict[str, Any], params: Dict[str, Any], scan_result: Dict[str, Any]) -> Dict[str, Any]:
    from workflow.detect_executor import execute_detect_on_scan_result

    return execute_detect_on_scan_result(ctx, params, scan_result)



def run_single_well_compensate(ctx: Dict[str, Any], params: Dict[str, Any], detect_result: Dict[str, Any]) -> Dict[str, Any]:
    from workflow.compensate_executor import execute_compensate_on_detect_result

    return execute_compensate_on_detect_result(ctx, params, detect_result)

def run_compensate_task(ctx: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
    detect_json = params.get("compensate_input_detect_json")
    if not detect_json:
        raise ValueError("独立 compensate 任务要求 compensate.input_detect_json 非空")

    detect_path = Path(detect_json)
    if not detect_path.exists():
        raise FileNotFoundError(f"未找到 detect_result.json: {detect_json}")

    detect_result = json.loads(detect_path.read_text(encoding="utf-8"))

    # 独立 compensate 建议只针对单孔 detect_result
    if "images" not in detect_result:
        raise ValueError("输入的 detect_result.json 不符合单孔 detect 结果格式，缺少 images 字段")

    return run_single_well_compensate(ctx, params, detect_result)

def _default_result_paths(base_save_dir: Path, well_name: str) -> Dict[str, str]:
    well_dir = base_save_dir / well_name
    return {
        "save_dir": str(well_dir / "images"),
        "scan_output_json": str(well_dir / "scan_result.json"),
        "detect_output_json": str(well_dir / "detect_result.json"),
        "compensate_output_json": str(well_dir / "compensate_result.json"),
    }



def _derive_well_ctx_params(base_ctx: Dict[str, Any], base_params: Dict[str, Any], well_name: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """为 well_list/full_plate 任务派生单孔上下文。"""
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



def _run_single_well_pipeline(ctx: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
    stages = params["stages"]
    stage_results: Dict[str, Any] = {}

    if "capture" in stages:
        stage_results["capture"] = run_single_well_capture(ctx, params)
    else:
        raise ValueError("当前 pipeline 版本要求 stages 至少包含 capture。")

    if "detect" in stages:
        stage_results["detect"] = run_single_well_detect(ctx, params, stage_results["capture"])

    if "compensate" in stages:
        if "detect" not in stage_results:
            raise ValueError("compensate 依赖 detect，请在 stages 中包含 detect。")
        stage_results["compensate"] = run_single_well_compensate(ctx, params, stage_results["detect"])

    result = {
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
    return result



def run_single_well_pipeline(ctx: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
    if not params.get("well_name"):
        raise ValueError("single_well 流程要求 well_name 非空")
    return _run_single_well_pipeline(ctx, params)



def run_well_list_pipeline(ctx: Dict[str, Any], params: Dict[str, Any], well_list: List[str]) -> Dict[str, Any]:
    base_save_dir = Path(params["save_dir"])
    wells: List[Dict[str, Any]] = []

    for well_name in well_list:
        well_ctx, well_params = _derive_well_ctx_params(ctx, params, well_name)
        well_result = _run_single_well_pipeline(well_ctx, well_params)
        wells.append(
            {
                "well_name": well_name,
                "capture_result_json": well_params["scan_output_json"],
                "detect_result_json": well_params.get("detect_output_json"),
                "compensate_result_json": well_params.get("compensate_output_json"),
                "result": well_result,
            }
        )

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



def main() -> None:
    parser = argparse.ArgumentParser(description="Run capture/detect/compensate pipeline task")
    parser.add_argument("--task", required=True, help="task json/yaml path")
    parser.add_argument("--camera", default=None)
    parser.add_argument("--objectives", default=None)
    parser.add_argument("--plates", default=None)
    parser.add_argument("--dump-json", default=None, help="output result json path")
    args = parser.parse_args()

    raw_task_cfg = load_structured_file(args.task)
    if "task" not in raw_task_cfg:
        raise KeyError(f"task 文件缺少顶层字段 'task': {args.task}")

    task = raw_task_cfg["task"]
    task_type = str(task.get("task_type") or "").strip().lower()
    if task_type not in {"capture", "pipeline", "compensate"}:
        raise ValueError("当前版本要求 task_type 为 capture , pipeline或compensate")

    from workflow.config_loader import load_runtime_context

    runtime_task_path, tmp_task_path = task_path_for_runtime_context(args.task)

    default_config_dir = PROJECT_ROOT / "config"
    camera_path = args.camera or str(default_config_dir / "camera.yaml")
    objectives_path = args.objectives or str(default_config_dir / "objectives.yaml")
    plates_path = args.plates or str(default_config_dir / "plates.yaml")

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

    params = build_pipeline_params(ctx)

    if task_type == "compensate":
        result = run_compensate_task(ctx, params)
    else:
        result = run_pipeline_task(ctx, params)

    save_result(
        result,
        args.dump_json or params.get("result_output_json") or params.get("compensate_output_json"),
    )

if __name__ == "__main__":
    main()
