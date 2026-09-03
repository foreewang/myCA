# 任务与命令行

任务文件顶层必须有 `task`。CLI 读 JSON 或 YAML；HTTP 请求体使用同一结构。

## 任务类型与范围

| `task_type` | 行为 |
| --- | --- |
| `capture` | 只采集，默认阶段 `capture` |
| `pipeline` | 可包含 `capture`、`detect`、`compensate` |
| `compensate` | 读已有检测结果，移动到目标中心 |
| `handoff` | 把 XY 位移台移动到对接点，供自控放置或取走培养板；`load_in` / `unload_out` |

| `observe_scope` | 孔位 |
| --- | --- |
| `single_well` | `target.well_name` |
| `well_list` | `target.well_list` |
| `full_plate` | 按板型展开全部孔 |

多孔任务时，每个孔的 `scan_result.json` / `detect_result.json` / `compensate_result.json` 会写到 `<capture.save_dir>/<well_name>/`。不要把单个 `detect.output_json` 当成多孔最终路径。总结果仍走 `output.result_json`。

## CLI

示例中的速度、坐标和从站来自当前设备。新电机必须先低速单轴测试并重新标定，不能把这些数直接用于首次运动。

采集任务，例如 `data/task_capture_single_well.json`：

```json
{
  "task": {
    "task_id": "capture_A1_local_001",
    "task_type": "capture",
    "plate_type": "24-well",
    "objective_name": "4x",
    "observe_scope": "single_well",
    "target": { "well_name": "A1" },
    "capture": {
      "save_dir": "/opt/colony_system/data/local_tasks/capture_A1_local_001/images",
      "filename_pattern": "{well}_{index:03d}_row{row:02d}_col{col:02d}.bmp"
    },
    "motion": {
      "port": "/dev/ttyUSB0",
      "baudrate": 115200,
      "x_slave": 1,
      "y_slave": 2,
      "profile_vel": 800000,
      "profile_acc": 800000,
      "profile_dec": 800000,
      "timeout_s": 120.0
    },
    "scan": {
      "overlap": 0.1,
      "use_objective_fov": true,
      "settle_s": 0.8,
      "output_json": "/opt/colony_system/data/local_tasks/capture_A1_local_001/scan_result.json"
    },
    "output": {
      "result_json": "/opt/colony_system/data/local_tasks/capture_A1_local_001/result.json"
    }
  }
}
```

```bash
cd /opt/colony_system
python workflow/run_task.py --task data/task_capture_single_well.json
```

覆盖配置路径：

```bash
python workflow/run_task.py \
  --task data/task_capture_single_well.json \
  --camera config/camera.yaml \
  --objectives config/objectives.yaml \
  --plates config/plates.yaml \
  --dump-json data/my_result.json
```

handoff 示例 `data/task_handoff_load_in.json`：

```json
{
  "task": {
    "task_id": "handoff_load_in_local_001",
    "task_type": "handoff",
    "plate_type": "24-well",
    "handoff": { "action": "load_in" },
    "output": {
      "result_json": "/opt/colony_system/data/local_tasks/handoff_load_in_local_001/result.json"
    }
  }
}
```

```bash
python workflow/run_task.py --task data/task_handoff_load_in.json --handoff config/handoff.yaml
```

拍照用 `capture` 任务验证。后台录像走 [HTTP API](http-api.md)，仓库没有单独的相机测试脚本。

## 常用字段

| 字段 | 说明 |
| --- | --- |
| `task_id` | 唯一 ID；HTTP 索引用它当文件名 |
| `task_type` | `capture` / `pipeline` / `compensate` / `handoff` |
| `plate_type` | 如 `24-well` |
| `objective_name` | 如 `4x`、`10x` |
| `objective` | 已废弃别名，新任务不要用 |
| `observe_scope` | `single_well` / `well_list` / `full_plate` |
| `target.well_name` / `target.well_list` | 目标孔 |
| `stages` | 如 `["capture", "detect"]` |
| `capture.save_dir` | 单孔图片目录；多孔为基础目录 |
| `capture.filename_pattern` | 图片名模板 |
| `motion.port` / `baudrate` | XY Modbus |
| `motion.x_slave` / `y_slave` | 默认 1 / 2 |
| `motion.profile_vel` / `profile_acc` / `profile_dec` | 采集和补偿必填 |
| `motion.timeout_s` | 单次 XY 超时，默认 120 秒 |
| `scan.overlap` | `0 <= overlap < 1` |
| `scan.use_objective_fov` | 是否用当前物镜视野算步长 |
| `scan.output_json` | 单孔 `scan_result.json` |
| `detect.entrypoint` | `模块路径:函数名`；省略则用 4x 模型入口 |
| `detect.model_dir` | 含 `model_manifest.json` 和校验过的 ONNX |
| `detect.provider` | `cuda` / `cpu` / `auto`；生产用 `cuda` |
| `detect.allow_cpu_fallback` | CUDA 失败是否重建 CPU session，默认关 |
| `detect.deduplication.calibrated` | 有重叠时必须为 `true` |
| `detect.deduplication.registration_tolerance_mm` | 跨视野配准容差，由标定集确定 |
| `detect.output_json` | 单孔检测 JSON；多孔会改写到孔目录 |
| `detect.save_overlay` | 默认开 |
| `detect.overlay_source` | `vision` 或 `workflow`，默认 `vision` |
| `detect.detect_well_border` | 默认开 |
| `detect.well_border_margin_mm` / `_px` | 靠近孔边缘的判定边距 |
| `compensate.selector` | 选哪个克隆 |
| `compensate.scale` | 如 `{ "x": 0.79, "y": 1.0 }` |
| `compensate.closed_loop` | 闭环复检 |
| `compensate.input_detect_json` | 独立补偿读取的检测结果 |
| `output.result_json` | 总结果 |
| `handoff.action` | `load_in`：位移台开到对接点，供自控放板；`unload_out`：开到对接点，供自控取板 |

曝光和增益来自 `camera.yaml` 的 `objective_settings.<objective_name>`。底层强制 Mono8，录像逐帧校验格式和长度。

检测入口、`is_pickable` 与 10x 对中见 [视觉检测](vision.md)。含 `detect` 的 HTTP 示意见 [HTTP API](http-api.md)。

## 补偿

### 选择器

`compensate.selector.mode`：

| mode | 说明 |
| --- | --- |
| `first` | 第一个有效克隆 |
| `largest_area` | 面积最大 |
| `nearest_image_center` | 离图像中心最近 |
| `clone_id` | 按 ID，可选 `image_index` |
| `image_and_clone` | 同时指定图号和 ID |

默认 `purpose` 是 `pick`，只选 `is_pickable=true`。4x 模型结果这项为 `false`。若只是把定位结果移到 10x 视野，设 `purpose: "10x_centering"`，只选 `eligible_for_10x_centering=true`。两种语义不混用。

独立补偿：

```json
{
  "compensate": {
    "input_detect_json": "/opt/colony_system/data/tasks/pipeline_C3_001/C3/detect_result.json",
    "selector": {
      "mode": "image_and_clone",
      "purpose": "10x_centering",
      "image_index": 4,
      "clone_id": "C01"
    }
  }
}
```

上例读取内置 4x 定位结果时，完整任务应使用 `objective_name: "10x"`，表示切换到 10x 后做视野对中。若输入结果确有经审核的 `is_pickable=true` 目标，才把 `purpose` 改为 `pick`。

### 闭环

第一次移动后再拍、再识别，判断是否继续。开启时必须有 `closed_loop.save_dir` 或任务里的 `capture.save_dir`。

```json
{
  "closed_loop": {
    "enabled": true,
    "save_dir": "/opt/colony_system/data/compensate_eval/closed_loop/C3_index04_c01",
    "filename_pattern": "closed_loop_{task_id}_{well}_iter{iteration:02d}.bmp",
    "max_iterations": 2,
    "tolerance_px": 10,
    "detect_entrypoint": "vision.vision.detect_pipeline:process_image",
    "selector": {
      "mode": "nearest_image_center",
      "purpose": "10x_centering"
    }
  }
}
```

只验证首次方向和距离时关掉闭环：

```json
{
  "closed_loop": {
    "enabled": false
  }
}
```

在 10x 上闭环复检需要显式 `detect_entrypoint`。省略时会走默认 4x 模型入口。

## 输出

### 采集 `scan_result.json`

- 参考点、视野、重叠率、点位数
- 每点目标坐标和实际运动
- 相机参数和图片路径
- 自动对焦决策，以及首次拍照前的对焦结果
- 运动安全检查

### 检测 `detect_result.json`

- 每张图的观察数、`review_candidates`
- 中心、面积、边框、相对图像中心偏移
- `confidence`、`is_valid_for_compensation`、`is_pickable`
- 孔边缘距离、`mm_per_pixel`
- `unique_clones` / `total_clone_count`（去重后唯一数）
- `overlay_image_path`

轮廓细化诊断（`refine_method` 等）不在 workflow 归一化结果里，在 vision 目录的 `07_result.json`。默认 overlay 是 vision 的 `06_overlay.bmp`。

模型流水线产物还包括实例标签图等，见 [视觉检测](vision.md)。旧规则算法调试目录常见：

```text
01_gray.bmp
02_coarse_flat.bmp
03_coarse_binary.bmp
04_refine_density.bmp
05_contour_mask.bmp
06_overlay.bmp
07_result.json
```

### 多孔目录

```text
data/some_task/
├─ C3/
│  ├─ images/
│  ├─ detect_overlays/
│  ├─ scan_result.json
│  └─ detect_result.json
├─ C5/
│  └─ ...
└─ result.json
```
