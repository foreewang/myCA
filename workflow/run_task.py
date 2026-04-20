from __future__ import annotations
import argparse, copy, json, sys, tempfile
from pathlib import Path
from typing import Any, Dict, List
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
import yaml

def load_structured_file(path: str | Path) -> Dict[str, Any]:
    path=Path(path); text=path.read_text(encoding='utf-8')
    return json.loads(text) if path.suffix.lower()=='.json' else (yaml.safe_load(text) or {})

def task_path_for_runtime_context(task_path: str | Path) -> tuple[str, str | None]:
    task_path=Path(task_path)
    if task_path.suffix.lower() != '.json': return str(task_path), None
    raw=load_structured_file(task_path)
    tmp=tempfile.NamedTemporaryFile(prefix='task_', suffix='.yaml', delete=False, mode='w', encoding='utf-8')
    yaml.safe_dump(raw, tmp, allow_unicode=True, sort_keys=False)
    tmp_path=tmp.name; tmp.close(); return tmp_path, tmp_path

def save_result(result: Dict[str, Any], dump_json: str | None) -> None:
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if dump_json:
        out_path=Path(dump_json); out_path.parent.mkdir(parents=True, exist_ok=True); out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')

def build_capture_params(ctx: Dict[str, Any]) -> Dict[str, Any]:
    task=ctx['task']; objective=ctx['objective']; camera=ctx['camera']
    scan_cfg=task.get('scan',{}) or {}; capture_cfg=task.get('capture',{}) or {}; target_cfg=task.get('target',{}) or {}; output_cfg=task.get('output',{}) or {}
    if scan_cfg.get('use_objective_fov', True):
        fov_w=objective['fov_mm']['width']; fov_h=objective['fov_mm']['height']
    else:
        fov_override=scan_cfg.get('fov_override_mm')
        if isinstance(fov_override, dict): fov_w=fov_override['width']; fov_h=fov_override['height']
        else: fov_w=fov_override; fov_h=fov_override
    return {'task_id': task['task_id'], 'task_type': task['task_type'], 'observe_scope': task.get('observe_scope'), 'plate_type': task['plate_type'], 'well_name': task.get('well_name') or target_cfg.get('well_name'), 'well_list': [str(x) for x in target_cfg.get('well_list', [])], 'objective_name': task['objective'], 'fov_mm': {'width': fov_w, 'height': fov_h}, 'resolution': camera['resolution'], 'device_index': camera.get('device_index', 0), 'exposure_us': camera.get('exposure_us'), 'gain': camera.get('gain'), 'save_dir': capture_cfg.get('save_dir'), 'filename_pattern': capture_cfg.get('filename_pattern'), 'overlap': scan_cfg.get('overlap'), 'settle_s': scan_cfg.get('settle_s', 0.8), 'scan_output_json': scan_cfg.get('output_json'), 'motion': task.get('motion', {}) or {}, 'result_output_json': output_cfg.get('result_json')}

def run_single_well_scan_capture(ctx: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
    from workflow.scan_planner import plan_single_well_scan
    from workflow.scan_executor import execute_scan_capture
    plan=plan_single_well_scan(ctx, params); return execute_scan_capture(ctx, params, plan)

def _default_result_paths(base_save_dir: Path, well_name: str) -> Dict[str, str]:
    well_dir=base_save_dir/well_name; return {'save_dir': str(well_dir/'images'), 'scan_output_json': str(well_dir/'scan_result.json')}

def _derive_well_ctx_params(base_ctx: Dict[str, Any], base_params: Dict[str, Any], well_name: str) -> tuple[Dict[str, Any], Dict[str, Any]]:
    well_ctx=copy.deepcopy(base_ctx); well_params=copy.deepcopy(base_params)
    well_ctx['task']['well_name']=well_name
    if 'target' in well_ctx['task'] and isinstance(well_ctx['task']['target'], dict): well_ctx['task']['target']['well_name']=well_name
    well_params['well_name']=well_name; well_params['task_id']=f"{base_params['task_id']}_{well_name}"
    base_save_dir=Path(base_params['save_dir']); defaults=_default_result_paths(base_save_dir, well_name)
    well_params['save_dir']=defaults['save_dir']; well_params['scan_output_json']=defaults['scan_output_json']
    return well_ctx, well_params

def run_well_list_scan_capture(ctx: Dict[str, Any], params: Dict[str, Any], well_list: List[str]) -> Dict[str, Any]:
    base_save_dir=Path(params['save_dir']); well_results=[]
    for well_name in well_list:
        well_ctx, well_params = _derive_well_ctx_params(ctx, params, well_name)
        scan_result = run_single_well_scan_capture(well_ctx, well_params)
        well_results.append({'well_name': well_name, 'scan_result_json': well_params['scan_output_json'], 'image_count': int(scan_result.get('image_count',0))})
    return {'task_id': params['task_id'], 'status': 'success', 'task_type': 'capture', 'observe_scope': 'well_list', 'plate_type': params['plate_type'], 'objective_name': params['objective_name'], 'base_save_dir': str(base_save_dir), 'well_count': len(well_results), 'wells': well_results}

def run_full_plate_scan_capture(ctx: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
    from workflow.plate_geometry import all_well_names
    wells=all_well_names(ctx['plate']); result=run_well_list_scan_capture(ctx, params, wells); result['observe_scope']='full_plate'; return result

def run_capture_task(ctx: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
    observe_scope=str(params.get('observe_scope') or '').lower()
    if observe_scope == 'single_well':
        if not params.get('well_name'): raise ValueError('observe_scope=single_well 时，target.well_name 不能为空')
        result=run_single_well_scan_capture(ctx, params); result['task_type']='capture'; result['observe_scope']='single_well'; return result
    if observe_scope == 'well_list':
        well_list=params.get('well_list') or []
        if not well_list: raise ValueError('observe_scope=well_list 时，target.well_list 不能为空')
        return run_well_list_scan_capture(ctx, params, well_list)
    if observe_scope == 'full_plate': return run_full_plate_scan_capture(ctx, params)
    raise ValueError(f'不支持的 observe_scope: {observe_scope}')

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--task', required=True, help='task json/yaml path')
    parser.add_argument('--camera', default=None)
    parser.add_argument('--objectives', default=None)
    parser.add_argument('--plates', default=None)
    parser.add_argument('--dump-json', default=None, help='output result json path')
    args = parser.parse_args()

    raw_task_cfg = load_structured_file(args.task)
    if 'task' not in raw_task_cfg:
        raise KeyError(f"task 文件缺少顶层字段 'task': {args.task}")

    task = raw_task_cfg['task']
    if task.get('task_type') != 'capture':
        raise ValueError('本次整理版仅保留采集流程，task_type 必须为 capture')

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

    params = build_capture_params(ctx)
    result = run_capture_task(ctx, params)
    save_result(
        result,
        args.dump_json or params.get('result_output_json') or params.get('scan_output_json')
    )
if __name__ == '__main__':
    main()
