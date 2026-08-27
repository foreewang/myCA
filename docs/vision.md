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

默认入口只允许 `objective_name=4x`。10x 检测必须显式写 legacy 入口。含 `detect` 的任务会在切镜和拍照前预检模型目录与 provider。

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

`registration_tolerance_mm` 要用带跨视野身份标注的 4x 扫描标定，不能凭单张图猜。未标定会在预检阶段失败，避免先跑完推理再报错。

`total_clone_count` 是去重后的唯一数，不是逐图观察数之和。逐图数在 `total_image_clone_count`。

## 单图调试

模型后端：

```powershell
python vision/run_detect.py path\to\image.bmp --backend model `
  --model-dir D:/colony_system/vision/models/ipsc_4x/production --provider cuda `
  --out-dir data/vision_debug
```

旧规则算法：

```powershell
python vision/run_detect.py path\to\image.bmp --backend legacy --out-dir data/vision_debug
```

任务 JSON 里的检测字段见 [任务与命令行](tasks.md)。
