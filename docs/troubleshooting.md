# 常见问题与维护

## 改了代码，测试结果没变

若 `uvicorn` 已在跑，改 `.py` 后必须重启服务。只改 JSON/YAML 或请求体通常不用重启。

## PowerShell 发 JSON 报错

用 `ConvertTo-Json` 和 `Invoke-RestMethod`，不要手写复杂转义。示例见 [HTTP API](http-api.md)。

## overlay 仍是黄框红点

多半是旧进程或旧任务产物。重启 uvicorn，重新提交，确认 `overlay_image_path` 指向 `*_vision/06_overlay.bmp`。

强制旧 workflow overlay：

```json
"detect": { "overlay_source": "workflow" }
```

## 自动对焦没执行

查 `config/autofocus.yaml`：

- `enabled` 是否 `true`
- `trigger.after_objective_switch` 是否开
- 本次物镜是否真的切换了
- 是否设了 `always_before_capture` 或 `always_before_capture_objectives`
- `trigger.scope` 是否为 `once_per_well` 或 `once_per_task`
- 当前是否为该孔第一个扫描点

策略说明见 [硬件与安全](hardware.md)。

## 闭环补偿报 save_dir

`compensate.closed_loop.enabled=true` 时必须有保存目录：

```json
"compensate": {
  "closed_loop": {
    "save_dir": "D:/colony_system/data/compensate_eval/closed_loop"
  }
}
```

或在任务中提供 `capture.save_dir`。闭环检测入口见 [任务与命令行](tasks.md)。

## 串口或相机被占用

确认没有其它进程占用同一 COM 口或 MVS 相机。

录像期间 workflow 复用共享录像相机。非录像时，autofocus 和正式采集避免提前打开相机抢句柄。

选错相机时查 `camera.yaml` 的 `serial_number`、`ip`、`device_index`。优先级：`serial_number` > `ip` > `device_index`。

## 配置校验失败

报错带字段路径，例如 `camera.objective_settings.10x`。按路径改 YAML，不要删校验器。用法见 [环境与配置](setup.md)。

## 检测在切镜前就失败

默认 4x 模型会在运动前预检：

- `objective_name` 必须是 `4x`
- 分辨率 5120×5120，且 `allow_downscale=false`
- `detect.model_dir` 必填
- `scan.overlap > 0` 时 `deduplication.calibrated=true`，并显式给出 `registration_tolerance_mm`
- 默认 `provider=cuda` 且不允许静默落到 CPU

契约见 [视觉检测](vision.md)。

## 维护习惯

- 改 `.py` → 重启 uvicorn → 再发任务
- 改 YAML → `python -m workflow.config_validator`
- 改相机、视觉、handoff、对焦 → 在开发支线跑完整测试，部署前再做配置校验和硬件分阶段验收
- 联调保留请求 JSON、`scan_result.json`、`detect_result.json`、`result.json`
- 标定文件建议进版本库；现场私有参数可用单独文件覆盖
- 功能 commit 不要混入大量 `data/` 运行产物
