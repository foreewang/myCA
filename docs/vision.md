# 视觉检测

workflow 经 `workflow.detect_api` 调用 vision，结果归一化为 `detect_result.json`。默认 overlay 是 vision 的 `06_overlay.bmp`，路径写在每张图的 `overlay_image_path`。

实现细节：[`vision/vision/README.md`](../vision/vision/README.md)。  
模型打包：[`vision/models/ipsc_4x/README.md`](../vision/models/ipsc_4x/README.md)。

## 默认入口

省略 `detect.entrypoint` 时使用 4x 模型实例定位：

```text
vision.vision.instance_pipeline:process_image
```

`detect.model_dir` 必须是含严格 manifest 和两份 ONNX 的发布目录。模型缺失、校验失败或运行时不兼容会让任务失败，不会变成“0 个克隆”，也不会静默回退规则算法。

旧入口仅在显式指定时可用：

```text
vision.vision.detect_pipeline:process_image
```

默认入口只允许 `objective_name=4x`。10x 检测必须显式写 legacy 入口。使用内置模型入口的 `detect` 任务会在切镜和拍照前预检模型目录与 provider；显式规则入口不执行该模型预检，但仍会检查重叠去重是否已标定。

## 规则算法的输出控制

规则算法、模型后端及默认入口的结构保持不变。使用规则算法时，任务仍须显式指定 `vision.vision.detect_pipeline:process_image`（兼容 `vision.detect_pipeline:process_image`）。`detect.save_debug` 只控制规则调试图，不选择后端，也不透传新的算法参数。

```json
{
  "detect": {
    "entrypoint": "vision.vision.detect_pipeline:process_image",
    "save_debug": false,
    "save_overlay": true,
    "overlay_source": "vision"
  }
}
```

上例是任务配置片段。`save_debug` 必须是布尔值，默认 `false`。规则入口有输出目录时，默认只写 `05_contour_mask.bmp`、`06_overlay.bmp` 和 `07_result.json`；设为 `true` 恢复 `01_gray.bmp`、`02_coarse_flat.bmp`、`03_coarse_binary.bmp`、`04_refine_density.bmp`，共 01–07 全部文件。检测结果及保留的 05/06 像素不变；5120×5120 图的 BMP 总量由约 200 MiB 降为约 100 MiB。

输出控制沿用原语义：`save_overlay=false` 时 workflow 不向规则入口传输出目录；`overlay_source=workflow` 时只由 workflow 生成自己的 overlay；直接调用规则 Python 入口并传 `out_dir=None` 时只返回内存结果。这些情况下 `save_debug=true` 也不会强制生成规则产物。`process_image` 未传 `out_dir` 时仍默认不落盘；`detect_from_gray` 仍默认 `out_dir=None`；`detect_from_path` 仍默认 `out_dir="outputs_5120_contour_refined_opt"`。模型和第三方入口不会收到 workflow 新增的 `save_debug` 参数。

同一输出目录里已有的 01–04 文件不会自动删除，切换为默认模式后这些历史文件仍可能存在。验收本次产物或体积时使用新的输出目录。

基准数据、验收工具及工控机同步/回退步骤见 [规则检测性能验收](rule_vision_performance.md)。该文档记录此前的性能优化验收。

孔壁功能已下线：不再检测孔圆或计算孔边距离，相关入参和返回字段已移除。`is_pickable` 表示目标自身有效；通用图像触边、质量过滤及模型 4x 定位和 10x 复核规则保持不变。

## 模型做什么、不做什么

- 只在 4x 图上检测、分割和定位可见 iPSC 克隆。
- 整图加重叠切片联合检测，再对 ROI 做实例分割。
- 正式实例与 `review_candidates` 分流；失败分割不进正式计数。
- 输出 16 位实例标签图、稳定 ID、模型版本/哈希和耗时。
- 跨重叠视野按物理坐标去重：逐图观察数与全孔唯一数分开。
- 4x 不判质量。`quality_assessment.status` 固定 `not_assessed`，质量在 10x 评估。

补偿默认 `purpose=pick`，只收 `is_pickable=true`。4x 结果该项为 `false`，避免把“已定位”当成“已完成 10x 质量确认”。

只把克隆移到 10x 视野时：

```json
"compensate": {
  "selector": { "purpose": "10x_centering", "mode": "first" }
}
```

此时只用 `eligible_for_10x_centering=true` 的正式分割实例。真正挑取仍用 `pick` + `is_pickable=true`。

## 重叠去重

`scan.overlap > 0` 时必须：

```json
"detect": {
  "deduplication": {
    "calibrated": true,
    "registration_tolerance_mm": 0.10,
    "intersection_over_min_threshold": 0.50
  }
}
```

`registration_tolerance_mm` 要用带跨视野身份标注的 4x 扫描标定，不能凭单张图猜。模型入口和规则入口都会在预检阶段拒绝未标定的重叠扫描，避免拍完整孔、跑完推理后再报错。

`total_clone_count` 是去重后的唯一数，不是逐图观察数之和。逐图数在 `total_image_clone_count`。

## 单图调试

模型后端：

```bash
python vision/run_detect.py path/to/image.bmp --backend model \
  --model-dir /opt/colony_system/vision/models/ipsc_4x/production --provider cuda \
  --out-dir data/vision_debug
```

旧规则算法：

```bash
python vision/run_detect.py path/to/image.bmp --backend legacy --out-dir data/vision_debug
```

上面的规则命令默认生成 05–07；需要全部调试产物时增加 `--save-debug`：

```bash
python vision/run_detect.py path/to/image.bmp --backend legacy --save-debug --out-dir data/vision_debug_full
```

`--save-debug` 仅支持 `--backend legacy`；CLI 默认仍为模型后端，未切换后端时使用该选项会报参数错误。规则 Python 入口可传同名布尔参数 `save_debug=True`，默认 `False`。

任务 JSON 里的检测字段见 [任务与命令行](tasks.md)。
