# 任务与命令行

任务文件顶层必须有 `task`。CLI 读 JSON 或 YAML；HTTP 请求体使用同一结构。

## 任务类型与范围


| `task_type`  | 行为                                                  |
| ------------ | --------------------------------------------------- |
| `capture`    | 只采集，默认阶段 `capture`                                  |
| `pipeline`   | 可包含 `capture`、`detect`、`compensate`                 |
| `compensate` | 读已有检测结果，移动到目标中心                                     |
| `handoff`    | 把 XY 位移台移动到对接点，供自控放置或取走培养板；`load_in` / `unload_out` |



| `observe_scope` | 孔位                 |
| --------------- | ------------------ |
| `single_well`   | `target.well_name` |
| `well_list`     | `target.well_list` |
| `full_plate`    | 按板型展开全部孔           |


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


| 字段                                                   | 说明                                                   |
| ---------------------------------------------------- | ---------------------------------------------------- |
| `task_id`                                            | 唯一 ID；HTTP 索引用它当文件名                                  |
| `task_type`                                          | `capture` / `pipeline` / `compensate` / `handoff`    |
| `plate_type`                                         | 如 `24-well`                                          |
| `objective_name`                                     | 如 `4x`、`10x`                                         |
| `objective`                                          | 已废弃别名，新任务不要用                                         |
| `observe_scope`                                      | `single_well` / `well_list` / `full_plate`           |
| `target.well_name` / `target.well_list`              | 目标孔                                                  |
| `stages`                                             | 如 `["capture", "detect"]`                            |
| `capture.save_dir`                                   | 单孔图片目录；多孔为基础目录                                       |
| `capture.filename_pattern`                           | 图片名模板                                                |
| `motion.port` / `baudrate`                           | XY Modbus                                            |
| `motion.x_slave` / `y_slave`                         | 默认 1 / 2                                             |
| `motion.profile_vel` / `profile_acc` / `profile_dec` | 采集和补偿必填                                              |
| `motion.timeout_s`                                   | 单次 XY 超时，默认 120 秒                                    |
| `scan.overlap`                                       | `0 <= overlap < 1`                                   |
| `scan.use_objective_fov`                             | 是否用当前物镜视野算步长                                         |
| `scan.output_json`                                   | 单孔 `scan_result.json`                                |
| `detect.entrypoint`                                  | `模块路径:函数名`；省略则用 4x 模型入口                              |
| `detect.model_dir`                                   | 含 `model_manifest.json` 和校验过的 ONNX                   |
| `detect.provider`                                    | `cuda` / `cpu` / `auto`；生产用 `cuda`                   |
| `detect.allow_cpu_fallback`                          | CUDA 失败是否重建 CPU session，默认关                          |
| `detect.deduplication.calibrated`                    | 有重叠时必须为 `true`                                       |
| `detect.deduplication.registration_tolerance_mm`     | 跨视野配准容差，由标定集确定                                       |
| `detect.output_json`                                 | 单孔检测 JSON；多孔会改写到孔目录                                  |
| `detect.save_overlay`                                | 默认开                                                  |
| `detect.save_debug`                                  | 规则入口专用布尔值，默认 `false`；有输出目录时只保存 05–07，`true` 恢复 01–07 |
| `detect.overlay_source`                              | `vision` 或 `workflow`，默认 `vision`                    |
| `compensate.selector`                                | 选哪个克隆                                                |
| `compensate.scale`                                   | 如 `{ "x": 0.79, "y": 1.0 }`                          |
| `compensate.closed_loop`                             | 闭环复检                                                 |
| `compensate.input_detect_json`                       | 独立补偿读取的检测结果                                          |
| `output.result_json`                                 | 总结果                                                  |
| `handoff.action`                                     | `load_in`：位移台开到对接点，供自控放板；`unload_out`：开到对接点，供自控取板    |


曝光和增益来自 `camera.yaml` 的 `objective_settings.<objective_name>`。底层强制 Mono8，录像逐帧校验格式和长度。

检测入口、`is_pickable` 与 10x 对中见 [视觉检测](vision.md)。含 `detect` 的 HTTP 示意见 [HTTP API](http-api.md)。

## 观察步骤计时

观察任务默认记录耗时，无需增加请求参数。所有 `timings_ms` 数值单位均为毫秒，
使用 `time.perf_counter()` 计算。计时期间只更新内存字典，随原有结果 JSON 保存。

### 在哪里查看

| 位置 | 内容 |
| --- | --- |
| 任务结果 `timings_ms` | 配置加载、识别预检、物镜准备、对焦策略加载、观察流程及 `task_work` |
| 单孔结果 `timings_ms` | `capture`、可选 `detect` / `compensate`、`well_total`；多孔时位于 `wells[].result` |
| `scan_result.json` 的 `timings_ms.scan_work` | 扫描执行开始到构建扫描结果的耗时 |
| `captures[].timings_ms` | 每个已完成采集点的移动、检查、对焦、相机打开、取图保存及点总耗时 |
| `point_timings[]` | 所有已进入的扫描点，包含点号、状态、步骤耗时和 `motion_timings_ms` |
| `captures[].motion_result.timings_ms` | 成功移动的内部计时，与对应 `point_timings[].motion_timings_ms` 相同 |

`point_timings[].status` 为 `success`、`failed` 或 `canceled`。移动、取图或取消检查
抛出异常时，扫描错误处理会保存当前点已执行步骤的耗时，随后继续抛出原异常。
未执行的步骤不出现。例如共享相机已打开时，不会记录该点的 `camera_open`。
拍照后收到取消请求时，图片仍保留在 `captures`，当前点状态为 `canceled`。

### 每点字段

| 字段 | 范围 |
| --- | --- |
| `move_total` | 整个 XY 移动调用，含串口会话、通信、轮询、稳定等待和校验 |
| `motion_guard` | 扫描层的移动误差及卡死检查 |
| `autofocus_total` | 完整自动对焦调用，包括内部采样及相机会话 |
| `camera_open` | 获取正式采集相机会话 |
| `capture_total` | 正式取帧及图片保存的外层调用，含进程通信 |
| `point_total` | 当前点的取消检查、进度处理和上述动作 |

`motion_timings_ms` 进一步提供 `position_snapshot`（移动前后快照累计）、
`axis_ready`、`write_targets`（双轴累计）、`command_gap`、`trigger`、
`wait_arrival`、`finish_control`（双轴累计）、`settle`、`command_snapshot`、
`arrival_check`。`wait_arrival` 包含运动及轮询通信，不能视为纯机械运动耗时。
`command_gap` 只包含目标参数写入之间及触发前的显式等待；辅助函数内的等待
计入所属辅助函数，例如 `axis_ready` 和 `trigger`。

### 统计和边界

父级计时包含子级，不可重复相加。例如 `point_total` 包含 `move_total`，
后者又包含 `settle`。`captures` 和 `point_timings` 中的每点计时也是同一份数据，
统计时只选一处。未归类的通信、连接开关和 Python 调度等时间仍包含在外层总耗时内。

下面的脚本统计成功扫描点的各步骤耗时；P95 使用最近秩定义。将文件路径替换为实际路径：

```python
import json
import math
import statistics
from collections import defaultdict

with open("data/example/scan_result.json", encoding="utf-8") as stream:
    result = json.load(stream)

samples = defaultdict(list)
for point in result.get("point_timings", []):
    if point["status"] != "success":
        continue
    for name, elapsed in point["timings_ms"].items():
        samples[name].append(elapsed)

for name, values in sorted(samples.items()):
    ordered = sorted(values)
    p95 = ordered[math.ceil(len(ordered) * 0.95) - 1]
    print(f"{name}: 次数={len(values)}, 总计={sum(values):.1f} ms, "
          f"均值={statistics.mean(values):.1f} ms, "
          f"中位数={statistics.median(values):.1f} ms, "
          f"P95={p95:.1f} ms, 最大={max(values):.1f} ms")
```

当前计时边界：

- `task_work` 从 `execute_task_request` 开始计时，不含排队及最终任务结果写盘。
- `scan_work` 不含扫描规划、扫描结果写盘及扫描末尾相机会话释放；这些工作包含在
  更外层的单孔 `capture` 中。多孔共享相机最终释放计入任务 `pipeline`。
- 对焦每次采样、SDK 取帧与图片写盘尚未分别计量，分别包含在 `autofocus_total`
  和 `capture_total`；串口连接开关包含在 `move_total`。
- 任务和单孔汇总计时在成功返回时附加；失败或取消时可查看已保存的扫描点计时。
  扫描规划前失败、进程被强制终止或结果磁盘不可写时，不保证有扫描计时文件。

## 补偿

### 可挑取且去重的候选文件

观察识别完成后额外生成 `pickable_detect_result.json`，保留原始检测结果。
新文件依据原结果的 `unique_clones[].source_detections` 分组，在每组内严格筛选
`is_pickable is True`，优先选择不触边、置信度高、距离图像中心近的观察记录，
最后按图片编号、克隆 ID 打破并列。原代表记录不可挑取时，会改选组内有效观察；
全组无有效观察则不输出。每个 `global_clone_id` 只输出一次，不重新编号。
这是现有算法分组范围内的去重，不代表对真实生物实例身份作了额外确认。

文件保留 `images[].clones[]` 及所属图片的原始坐标、比例尺、图片路径等信息，可直接作为
`compensate.input_detect_json`，使用 `purpose="pick"` 和原始 `image_index + clone_id`。
不对不同图片的像素坐标求平均。没有候选时仍生成空 `images` 和零计数。
`total_image_clone_count`、`total_clone_count`、`unique_clone_count` 均表示输出候选数；
`candidate_groups` 记录各候选的代表图片/克隆、原始来源 ID 和可挑取观察数，
`source_deduplication` 保存原去重元数据。原来的 `unique_clones` 汇总不直接复制，避免代表坐标过期。

单孔可指定 `task.detect.pickable_output_json`；省略时在 `detect.output_json`（或
`output.detect_json`）同目录生成，未配置检测输出路径时使用 `capture.save_dir`。
多孔固定在 `<capture.save_dir>/<well_name>/pickable_detect_result.json` 生成，覆盖单孔路径配置。
候选路径不得与检测、扫描、补偿或总结果文件相同。该文件不受总结果的 `persist_result` 控制。
检测结果及完成任务的孔记录通过 `pickable_result_json` 提供实际路径。

已有检测结果也可以离线转换，原文件保持不变：

```bash
python -m workflow.pickable_result --input data/task/B2/detect_result.json
```

可选 `--output` 指定目标路径。缺失去重分组、重复来源 ID 或分组与目标不一致时会报错，
不会把缺失去重信息的记录视为独立目标直接导出。



### 选择器

`compensate.selector.mode`：


| mode                   | 说明                    |
| ---------------------- | --------------------- |
| `first`                | 第一个有效克隆               |
| `largest_area`         | 面积最大                  |
| `nearest_image_center` | 离图像中心最近               |
| `clone_id`             | 按 ID，可选 `image_index` |
| `image_and_clone`      | 同时指定图号和 ID            |


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
- `mm_per_pixel`
- `unique_clones` / `total_clone_count`（去重后唯一数）
- `overlay_image_path`

轮廓细化诊断（`refine_method` 等）不在 workflow 归一化结果里，在 vision 目录的 `07_result.json`。默认 overlay 是 vision 的 `06_overlay.bmp`。

模型流水线产物还包括实例标签图等，见 [视觉检测](vision.md)。规则算法有输出目录时默认只生成：

```text
05_contour_mask.bmp
06_overlay.bmp
07_result.json
```

`task.detect.save_debug=true` 可恢复以下全部调试产物。该字段必须是布尔值，省略时为 `false`；规则识别结果及 05/06 像素不变，5120×5120 的 BMP 总量约减半（200 MiB → 100 MiB）。

```text
01_gray.bmp
02_coarse_flat.bmp
03_coarse_binary.bmp
04_refine_density.bmp
05_contour_mask.bmp
06_overlay.bmp
07_result.json
```

规则任务仍须显式设置 `detect.entrypoint="vision.vision.detect_pipeline:process_image"`（或 `vision.detect_pipeline:process_image`），省略入口仍使用原模型后端。`save_debug` 仅转发到这两个规则入口，不改变模型结构、结果接口或其他算法参数的转发。

`save_overlay=false`、`overlay_source=workflow` 不会因打开 `save_debug` 而生成规则产物；后者仍只生成 workflow overlay。规则 Python 入口的 `out_dir=None` 继续只返回内存结果；`process_image` 未传 `out_dir` 时仍不落盘，`detect_from_gray` 仍默认 `out_dir=None`，`detect_from_path` 仍默认 `out_dir="outputs_5120_contour_refined_opt"`。单图完整调试命令为 `python vision/run_detect.py image.bmp --backend legacy --save-debug --out-dir data/vision_debug_full`；`--save-debug` 仅限 legacy，CLI 默认后端保持不变。同目录里旧有的 01–04 不自动删除，比较产物和体积应使用新的输出目录。

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
