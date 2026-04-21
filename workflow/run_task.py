from __future__ import annotations

import argparse
import copy
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List

# 约定项目根目录为当前脚本所在目录的上两级。
# 这样做的目的是兼容“直接运行脚本”的场景，确保可以稳定导入 workflow 包。
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import yaml


def load_structured_file(path: str | Path) -> Dict[str, Any]:
    """
    读取结构化配置文件，支持 JSON 和 YAML。

    参数
    ----
    path : str | Path
        文件路径。

    返回
    ----
    Dict[str, Any]
        解析后的字典对象。若 YAML 为空，则返回空字典。

    说明
    ----
    这个函数的作用是统一任务文件读取入口，避免上层分别处理
    json.loads 和 yaml.safe_load。
    """
    path = Path(path)
    text = path.read_text(encoding='utf-8')
    return json.loads(text) if path.suffix.lower() == '.json' else (yaml.safe_load(text) or {})



def task_path_for_runtime_context(task_path: str | Path) -> tuple[str, str | None]:
    """
    为 runtime context 准备任务文件路径。

    参数
    ----
    task_path : str | Path
        原始任务文件路径，可为 JSON 或 YAML。

    返回
    ----
    tuple[str, str | None]
        - 第一个返回值：真正传给 load_runtime_context 的任务文件路径
        - 第二个返回值：若过程中创建了临时文件，则返回临时文件路径；否则返回 None

    说明
    ----
    当前下游的 load_runtime_context 读取接口以 YAML 为主要使用方式。
    因此如果输入是 JSON，这里会先把 JSON 转成临时 YAML，再交给下游加载。

    这样做的价值是：
    - 上层任务下发仍然可以用 JSON
    - 下游配置装配逻辑保持统一
    """
    task_path = Path(task_path)

    # 若原始任务已经是 YAML，则无需转换。
    if task_path.suffix.lower() != '.json':
        return str(task_path), None

    raw = load_structured_file(task_path)

    # 将 JSON 任务写成临时 YAML 文件，供 runtime context 使用。
    tmp = tempfile.NamedTemporaryFile(
        prefix='task_',
        suffix='.yaml',
        delete=False,
        mode='w',
        encoding='utf-8',
    )
    yaml.safe_dump(raw, tmp, allow_unicode=True, sort_keys=False)
    tmp_path = tmp.name
    tmp.close()
    return tmp_path, tmp_path



def save_result(result: Dict[str, Any], dump_json: str | None) -> None:
    """
    输出任务结果，并按需写入 JSON 文件。

    参数
    ----
    result : Dict[str, Any]
        最终结果字典。
    dump_json : str | None
        结果输出文件路径。若为 None，则仅打印到终端。

    说明
    ----
    这里统一承担两件事：
    1. 把结果打印到标准输出，方便调试和人工查看；
    2. 若指定输出路径，则把结果持久化为 JSON，供下游流程继续读取。
    """
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if dump_json:
        out_path = Path(dump_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding='utf-8',
        )



def build_capture_params(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """
    从运行时上下文中提取采集任务真正需要的参数。

    参数
    ----
    ctx : Dict[str, Any]
        由 config_loader.load_runtime_context 返回的统一上下文字典。

    返回
    ----
    Dict[str, Any]
        平铺后的采集参数字典。

    说明
    ----
    task / camera / objective / output 中的配置字段原本是分散的。
    这个函数的作用就是把它们整理成执行层更容易使用的一份 params。

    这里会处理几个关键逻辑：
    - 视野尺寸优先使用物镜配置中的 fov_mm
    - 若任务显式要求覆盖，则使用 scan.fov_override_mm
    - 提取保存目录、命名模板、重叠率、运动参数、输出路径等
    """
    task = ctx['task']
    objective = ctx['objective']
    camera = ctx['camera']

    scan_cfg = task.get('scan', {}) or {}
    capture_cfg = task.get('capture', {}) or {}
    target_cfg = task.get('target', {}) or {}
    output_cfg = task.get('output', {}) or {}

    # 视野尺寸默认跟随物镜配置；仅当任务显式指定覆盖时才改用覆盖值。
    if scan_cfg.get('use_objective_fov', True):
        fov_w = objective['fov_mm']['width']
        fov_h = objective['fov_mm']['height']
    else:
        fov_override = scan_cfg.get('fov_override_mm')
        if isinstance(fov_override, dict):
            fov_w = fov_override['width']
            fov_h = fov_override['height']
        else:
            fov_w = fov_override
            fov_h = fov_override

    return {
        'task_id': task['task_id'],
        'task_type': task['task_type'],
        'observe_scope': task.get('observe_scope'),
        'plate_type': task['plate_type'],
        'well_name': task.get('well_name') or target_cfg.get('well_name'),
        'well_list': [str(x) for x in target_cfg.get('well_list', [])],
        'objective_name': task['objective'],
        'fov_mm': {'width': fov_w, 'height': fov_h},
        'resolution': camera['resolution'],
        'device_index': camera.get('device_index', 0),
        'exposure_us': camera.get('exposure_us'),
        'gain': camera.get('gain'),
        'save_dir': capture_cfg.get('save_dir'),
        'filename_pattern': capture_cfg.get('filename_pattern'),
        'overlap': scan_cfg.get('overlap'),
        'settle_s': scan_cfg.get('settle_s', 0.8),
        'scan_output_json': scan_cfg.get('output_json'),
        'motion': task.get('motion', {}) or {},
        'result_output_json': output_cfg.get('result_json'),
    }



def run_single_well_scan_capture(ctx: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
    """
    执行单孔扫描采集。

    处理流程：
    1. 调用 scan_planner 生成该孔的扫描点位计划；
    2. 调用 scan_executor 按计划执行位移与拍照；
    3. 返回单孔采集结果。
    """
    from workflow.scan_planner import plan_single_well_scan
    from workflow.scan_executor import execute_scan_capture

    plan = plan_single_well_scan(ctx, params)
    return execute_scan_capture(ctx, params, plan)



def _default_result_paths(base_save_dir: Path, well_name: str) -> Dict[str, str]:
    """
    为某个孔生成默认输出路径。

    返回内容包括：
    - 当前孔图片目录
    - 当前孔扫描结果 JSON 路径
    """
    well_dir = base_save_dir / well_name
    return {
        'save_dir': str(well_dir / 'images'),
        'scan_output_json': str(well_dir / 'scan_result.json'),
    }



def _derive_well_ctx_params(
    base_ctx: Dict[str, Any],
    base_params: Dict[str, Any],
    well_name: str,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """
    从任务级上下文派生出某个孔专用的 ctx 和 params。

    说明
    ----
    多孔/整板任务本质上可以拆分成多个单孔任务。
    这里通过深拷贝，为每个孔构造独立执行上下文，避免不同孔之间互相污染。
    """
    well_ctx = copy.deepcopy(base_ctx)
    well_params = copy.deepcopy(base_params)

    # 把孔名写回 task 与 params，保证上下文一致。
    well_ctx['task']['well_name'] = well_name
    if 'target' in well_ctx['task'] and isinstance(well_ctx['task']['target'], dict):
        well_ctx['task']['target']['well_name'] = well_name

    # 给每个孔派生出自己的 task_id，便于结果追踪。
    well_params['well_name'] = well_name
    well_params['task_id'] = f"{base_params['task_id']}_{well_name}"

    # 为当前孔设置默认输出目录和扫描结果路径。
    base_save_dir = Path(base_params['save_dir'])
    defaults = _default_result_paths(base_save_dir, well_name)
    well_params['save_dir'] = defaults['save_dir']
    well_params['scan_output_json'] = defaults['scan_output_json']
    return well_ctx, well_params



def run_well_list_scan_capture(ctx: Dict[str, Any], params: Dict[str, Any], well_list: List[str]) -> Dict[str, Any]:
    """
    执行多孔扫描采集。

    参数
    ----
    ctx : Dict[str, Any]
        运行时上下文。
    params : Dict[str, Any]
        任务级参数。
    well_list : List[str]
        待执行的孔位列表。

    返回
    ----
    Dict[str, Any]
        多孔任务结果汇总。

    说明
    ----
    这个函数的策略很简单：
    逐孔派生参数 -> 逐孔调用单孔扫描 -> 汇总结果。
    """
    base_save_dir = Path(params['save_dir'])
    well_results = []

    for well_name in well_list:
        well_ctx, well_params = _derive_well_ctx_params(ctx, params, well_name)
        scan_result = run_single_well_scan_capture(well_ctx, well_params)
        well_results.append({
            'well_name': well_name,
            'scan_result_json': well_params['scan_output_json'],
            'image_count': int(scan_result.get('image_count', 0)),
        })

    return {
        'task_id': params['task_id'],
        'status': 'success',
        'task_type': 'capture',
        'observe_scope': 'well_list',
        'plate_type': params['plate_type'],
        'objective_name': params['objective_name'],
        'base_save_dir': str(base_save_dir),
        'well_count': len(well_results),
        'wells': well_results,
    }



def run_full_plate_scan_capture(ctx: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
    """
    执行整板扫描采集。

    实现方式：
    先根据板型配置生成全部孔位名，再复用 run_well_list_scan_capture。
    """
    from workflow.plate_geometry import all_well_names

    wells = all_well_names(ctx['plate'])
    result = run_well_list_scan_capture(ctx, params, wells)
    result['observe_scope'] = 'full_plate'
    return result



def run_capture_task(ctx: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
    """
    根据 observe_scope 分发采集任务。

    支持三种范围：
    - single_well: 单孔
    - well_list: 多孔列表
    - full_plate: 整板

    说明
    ----
    这是这个脚本在业务层面的核心分发入口。
    """
    observe_scope = str(params.get('observe_scope') or '').lower()

    if observe_scope == 'single_well':
        if not params.get('well_name'):
            raise ValueError('observe_scope=single_well 时，target.well_name 不能为空')
        result = run_single_well_scan_capture(ctx, params)
        result['task_type'] = 'capture'
        result['observe_scope'] = 'single_well'
        return result

    if observe_scope == 'well_list':
        well_list = params.get('well_list') or []
        if not well_list:
            raise ValueError('observe_scope=well_list 时，target.well_list 不能为空')
        return run_well_list_scan_capture(ctx, params, well_list)

    if observe_scope == 'full_plate':
        return run_full_plate_scan_capture(ctx, params)

    raise ValueError(f'不支持的 observe_scope: {observe_scope}')



def main() -> None:
    """
    命令行入口。

    功能概括：
    1. 解析任务文件和配置文件路径；
    2. 读取并校验任务内容；
    3. 组装运行时上下文；
    4. 构造采集参数；
    5. 根据 observe_scope 执行单孔/多孔/整板采集；
    6. 输出结果并按需写盘。

    说明
    ----
    从你目前整个项目结构来看，这个脚本可以视为“采集流程主入口”。
    上层调度、手工命令行测试，都会先到这里，然后再分发到：
    - config_loader
    - plate_geometry / scan_planner
    - stage_executor
    - camera_executor
    - scan_executor
    """
    parser = argparse.ArgumentParser()
    parser.add_argument('--task', required=True, help='task json/yaml path')
    parser.add_argument('--camera', default=None)
    parser.add_argument('--objectives', default=None)
    parser.add_argument('--plates', default=None)
    parser.add_argument('--dump-json', default=None, help='output result json path')
    args = parser.parse_args()

    # 先读取任务原文，用于做最基础的结构校验。
    raw_task_cfg = load_structured_file(args.task)
    if 'task' not in raw_task_cfg:
        raise KeyError(f"task 文件缺少顶层字段 'task': {args.task}")

    task = raw_task_cfg['task']

    # 当前这个整理版脚本只保留了采集流程入口。
    if task.get('task_type') != 'capture':
        raise ValueError('本次整理版仅保留采集流程，task_type 必须为 capture')

    from workflow.config_loader import load_runtime_context

    # 若任务是 JSON，则转成临时 YAML，供 runtime context 统一装配。
    runtime_task_path, tmp_task_path = task_path_for_runtime_context(args.task)

    default_config_dir = PROJECT_ROOT / 'config'
    camera_path = args.camera or str(default_config_dir / 'camera.yaml')
    objectives_path = args.objectives or str(default_config_dir / 'objectives.yaml')
    plates_path = args.plates or str(default_config_dir / 'plates.yaml')

    try:
        # 装配统一运行时上下文：task + camera + objective + plate
        ctx = load_runtime_context(
            task_path=runtime_task_path,
            camera_path=camera_path,
            objectives_path=objectives_path,
            plates_path=plates_path,
        )
    finally:
        # 若创建过临时 YAML，则收尾删除。
        if tmp_task_path:
            try:
                Path(tmp_task_path).unlink(missing_ok=True)
            except Exception:
                pass

    # 从上下文中整理出执行层真正需要的平铺参数。
    params = build_capture_params(ctx)

    # 根据观察范围分发到对应采集逻辑。
    result = run_capture_task(ctx, params)

    # 优先级：命令行显式指定 > output.result_json > scan.output_json
    save_result(
        result,
        args.dump_json or params.get('result_output_json') or params.get('scan_output_json'),
    )


if __name__ == '__main__':
    main()

