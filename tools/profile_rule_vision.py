"""Run the real rule entrypoint and report non-overlapping wall time per stage.

Instrumentation is scoped to this serial benchmark process; production code and
result JSON are not changed. CUDA texture time includes its synchronous host
transfer, hence initialization/compilation on the first call is included too.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from contextlib import contextmanager, ExitStack
import csv
from datetime import datetime
from functools import wraps
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import sys
from time import perf_counter
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

STAGES = {
    'read_input': '读取图片文件',
    'decode': '图片解码',
    'gray_normalize': '灰度化与格式统一',
    'input_other': '输入管理与缓冲区释放',
    'coarse_resize': '整图缩放',
    'coarse_texture_compute': '粗检纹理计算（含GPU传输/同步）',
    'coarse_threshold': '粗检阈值、种子与形态学',
    'coarse_candidates': '粗候选提取、质量过滤与排序',
    'coarse_initial_point': '粗候选内部点',
    'roi_resize': 'ROI工作图缩放',
    'roi_texture_compute': 'ROI纹理计算（含GPU传输/同步）',
    'roi_threshold': 'ROI阈值、种子与形态学',
    'roi_contour': 'ROI关联、轮廓提取及掩膜恢复',
    'roi_grabcut': '可选GrabCut细化',
    'roi_safe_point': 'ROI内部定位点与距离计算',
    'coordinate_result': '坐标变换与结果字段整理',
    'render_overlay': '轮廓、定位点与比例尺绘制',
    'save_mask': '05掩膜编码与写盘',
    'save_overlay': '06叠加图编码与写盘',
    'save_debug': '01–04调试图编码与写盘',
    'save_json': '07结果JSON序列化与写盘',
    'output_other': '输出目录与输出数据准备',
    'pipeline_other': '流水线其他（扩框裁ROI、全图分配/合并、计时开销等）',
}


class StageProfiler:
    def __init__(self, clock=perf_counter):
        self.clock = clock
        self.phase = ''
        self.stack = []
        self.seconds = defaultdict(float)
        self.calls = defaultdict(int)
        self.events = []

    @contextmanager
    def span(self, name, phase=None):
        old_phase = self.phase
        if phase is not None:
            self.phase = phase
        frame = {'start': self.clock(), 'children': 0.0}
        self.stack.append(frame)
        try:
            yield
        finally:
            elapsed = self.clock() - frame['start']
            self.stack.pop()
            exclusive = elapsed - frame['children']
            self.seconds[name] += exclusive
            self.calls[name] += 1
            self.events.append({'stage': name, 'inclusive_s': elapsed, 'exclusive_s': exclusive})
            if self.stack:
                self.stack[-1]['children'] += elapsed
            self.phase = old_phase

    def wrap(self, original, stage, phase=None):
        @wraps(original)
        def measured(*args, **kwargs):
            name = stage(*args, **kwargs) if callable(stage) else stage
            if name is None:
                return original(*args, **kwargs)
            with self.span(name, phase):
                return original(*args, **kwargs)
        return measured

    @contextmanager
    def install(self):
        from vision.vision import detect_pipeline as pipeline
        from vision.vision import image_loader, postprocess, preprocess, segment, texture_segment as ts
        import cv2
        import numpy as np

        bindings = [
            (np, 'fromfile', 'read_input', None),
            (cv2, 'imdecode', 'decode', None),
            (image_loader, 'to_gray_u8', 'gray_normalize', None),
            (pipeline, 'load_gray_image', 'input_other', None),
            (pipeline, 'detect_coarse_rois', 'coarse_candidates', 'coarse'),
            (pipeline, 'refine_contour_in_roi', 'roi_contour', 'roi'),
            (ts, 'resize_keep_ratio', lambda *a, **k: self.phase + '_resize', None),
            (ts, 'texture_signal', lambda *a, **k: self.phase + '_threshold', None),
            (preprocess, 'texture_moments', lambda *a, **k: self.phase + '_texture_compute', None),
            (ts, 'safe_texture_point', lambda *a, **k: 'coarse_initial_point' if self.phase == 'coarse' else 'roi_safe_point', None),
            (segment, '_edge_refine_mask_grabcut', 'roi_grabcut', None),
            (ts, 'map_points', 'coordinate_result', None),
            (ts, 'map_bbox', 'coordinate_result', None),
            (pipeline, 'to_global_contour', 'coordinate_result', None),
            (pipeline, 'build_refined_component', 'coordinate_result', None),
            (pipeline, 'build_failed_component', 'coordinate_result', None),
            (pipeline, 'save_outputs', 'output_other', None),
            (postprocess, 'draw_scale_bar', 'render_overlay', None),
            (postprocess, 'save_image', lambda path, *a, **k: {
                '05_contour_mask.bmp': 'save_mask', '06_overlay.bmp': 'save_overlay',
            }.get(Path(path).name, 'save_debug'), None),
            (postprocess, 'atomic_write_json', 'save_json', None),
        ]
        # OpenCV is shared by segmentation: only count top-level pipeline drawing
        # as overlay work, leaving internal mask drawing in its owning stage.
        for name in ('drawContours', 'rectangle', 'circle', 'putText'):
            bindings.append((cv2, name, lambda *a, **k: 'render_overlay' if not self.phase else None, None))
        with ExitStack() as stack:
            for module, name, stage, phase in bindings:
                stack.enter_context(patch.object(module, name, self.wrap(getattr(module, name), stage, phase)))
            yield


def profile_image(path, out_dir, *, backend=None, save_debug=False):
    from vision.vision.detect_pipeline import process_image
    profiler = StageProfiler()
    kwargs = {'out_dir': out_dir, 'save_debug': save_debug}
    if backend is not None:
        kwargs['texture_backend'] = backend
    error = None
    result = None
    with profiler.install():
        with profiler.span('pipeline_other'):
            try:
                result = process_image(path, **kwargs)
            except Exception as exc:
                error = f'{type(exc).__name__}: {exc}'
    total = profiler.events[-1]['inclusive_s']
    record = {
        'image': Path(path).name, 'input_path': str(path), 'output_dir': str(out_dir),
        'total_s': total, 'stage_seconds': {stage: profiler.seconds[stage] for stage in STAGES},
        'stage_calls': {stage: profiler.calls[stage] for stage in STAGES},
        'events': profiler.events, 'error': error,
        'component_count': result['component_count'] if result else 0,
        'segmented_count': sum(bool(c['contour_points']) for c in result['components']) if result else 0,
        'pickable_count': sum(c.get('is_pickable') is True for c in result['components']) if result else 0,
        'actual_backend': result['texture_processing']['coarse_backend'] if result else None,
    }
    if abs(sum(record['stage_seconds'].values()) - total) > 1e-6:
        raise AssertionError('Exclusive stage accounting does not match total time')
    return record, result


def save_reports(run_dir, report):
    (run_dir / 'stage_timing.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    with (run_dir / 'per_image_timing.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['round', 'image', 'backend', 'components', 'segmented', 'pickable', 'total_s',
                         *[f'{stage}_s' for stage in STAGES], 'error'])
        for record in report['images']:
            writer.writerow([record['round'], record['image'], record['actual_backend'], record['component_count'],
                             record['segmented_count'], record['pickable_count'], record['total_s'],
                             *record['stage_seconds'].values(), record['error']])
    lines = ['# 当前规则视觉分步骤计时', '', f"运行目录：{run_dir}", '',
             f"后端请求：{report['backend_request']}；预热次数：{report['warmup']}；每轮图片数：{report['image_count']}", '',
             '计时单位为秒，各步骤为排除子步骤后的墙钟时间，可相加；同一图片的多个ROI耗时累加。',
             'GPU纹理计算包含上传、计算和同步回传，首次运行还包含CUDA导入、初始化与编译。',
             'pipeline_other包含未独立计时的扩框裁ROI、全图缓冲区分配/合并以及计时自身开销，不将这些时间误称为纯裁剪耗时。',
             '零耗时表示该图/该轮没有执行这个步骤，例如默认未开启GrabCut或没有ROI。', '',
             '| 步骤 | ' + ' | '.join(f"第{r['round']}轮" for r in report['rounds']) + ' |',
             '|---|' + '---:|' * len(report['rounds'])]
    for stage, label in STAGES.items():
        lines.append('| ' + label + ' | ' + ' | '.join(f"{r['stage_seconds'][stage]:.6f}" for r in report['rounds']) + ' |')
    lines.append('| 图片处理总耗时 | ' + ' | '.join(f"{r['image_total_s']:.6f}" for r in report['rounds']) + ' |')
    lines += ['', f"全部图片调用合计：{report['image_total_s']:.6f}秒。",
              f"测试脚本墙钟（到报告写出前）：{report['script_before_report_s']:.6f}秒，包含导入、准备、预热（如有）及循环记录。", '',
              '每张图片和每次函数调用的明细见per_image_timing.csv和stage_timing.json。',
              '检测输出含失败候选，component_count不是成功识别数；本工具不评估生物学准确率。']
    (run_dir / 'stage_timing.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')


def main():
    script_start = perf_counter()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--images', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True, help='Parent directory; a unique timestamp subdirectory is created')
    parser.add_argument('--backend', choices=['cpu', 'cuda', 'auto'], default=None, help='Omit to test current algorithm default')
    parser.add_argument('--repeats', type=int, default=1)
    parser.add_argument('--warmup', type=int, default=0, help='Extra unmeasured calls on first image; default 0 includes cold start')
    parser.add_argument('--save-debug', action='store_true')
    args = parser.parse_args()
    if args.repeats < 1 or args.warmup < 0:
        parser.error('repeats must be >= 1 and warmup must be >= 0')
    if not args.images.is_dir():
        parser.error(f'Input directory does not exist: {args.images}')
    files = sorted(p for p in args.images.iterdir() if p.suffix.lower() in {'.bmp', '.png', '.jpg', '.jpeg', '.tif', '.tiff'} and p.is_file())
    if not files:
        parser.error('No supported input images')
    run_dir = args.out.resolve() / ('vision_stage_timing_' + datetime.now().strftime('%Y%m%d_%H%M%S_%f'))
    run_dir.mkdir(parents=True, exist_ok=False)
    import cv2
    import numpy as np
    from vision.vision.detect_pipeline import process_image
    from vision.vision.gpu_ops import backend_status, DEFAULT_TEXTURE_BACKEND
    manifest = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted((ROOT / 'vision' / 'vision').glob('*.py'))}
    manifest['tools/profile_rule_vision.py'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    report = {'images_dir': str(args.images.resolve()), 'run_dir': str(run_dir),
              'backend_request': args.backend or f'default ({DEFAULT_TEXTURE_BACKEND})',
              'warmup': args.warmup, 'image_count': len(files), 'repeats': args.repeats,
              'environment': {'python': sys.version, 'executable': sys.executable, 'platform': platform.platform(),
                              'opencv': cv2.__version__, 'numpy': np.__version__},
              'code_sha256': manifest, 'images': [], 'rounds': []}
    try:
        report['environment']['cupy'] = importlib.metadata.version('cupy-cuda12x')
    except importlib.metadata.PackageNotFoundError:
        pass
    warmup_kwargs = {} if args.backend is None else {'texture_backend': args.backend}
    warmup_start = perf_counter()
    for _ in range(args.warmup):
        process_image(files[0], **warmup_kwargs)
    report['warmup_s'] = perf_counter() - warmup_start
    print(f'Output: {run_dir}', flush=True)
    for iteration in range(1, args.repeats + 1):
        round_records = []
        for index, path in enumerate(files, 1):
            # Only files within this newly created run are reused on subsequent
            # rounds. Reports retain each round; images show the final round.
            record, _ = profile_image(path, run_dir / 'images' / f'{path.stem}_{path.suffix[1:].lower()}',
                                      backend=args.backend, save_debug=args.save_debug)
            record.update(round=iteration, index=index)
            report['images'].append(record)
            round_records.append(record)
            print(f"round={iteration} {index}/{len(files)} {path.name}: {record['total_s']:.4f}s "
                  f"backend={record['actual_backend']} candidates={record['component_count']} "
                  f"segmented={record['segmented_count']} pickable={record['pickable_count']} "
                  f"error={record['error']}", flush=True)
        report['rounds'].append({'round': iteration,
            'image_total_s': sum(r['total_s'] for r in round_records),
            'stage_seconds': {stage: sum(r['stage_seconds'][stage] for r in round_records) for stage in STAGES}})
    report['gpu'] = backend_status()
    report['image_total_s'] = sum(r['total_s'] for r in report['images'])
    report['script_before_report_s'] = perf_counter() - script_start
    save_reports(run_dir, report)
    print(json.dumps({'output': str(run_dir), 'round_total_s': [r['image_total_s'] for r in report['rounds']],
                      'image_total_s': report['image_total_s'], 'script_before_report_s': report['script_before_report_s']}, ensure_ascii=False), flush=True)
    return 1 if any(r['error'] for r in report['images']) else 0


if __name__ == '__main__':
    raise SystemExit(main())
