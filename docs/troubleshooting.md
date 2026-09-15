# 常见问题与维护

## 改了代码，测试结果没变

若 `uvicorn` 已在跑，改 `.py` 后必须重启服务。只改 JSON/YAML 或请求体通常不用重启。

## curl 发 JSON 报错

用文件或 here-doc 提交 JSON，不要在 shell 里手写复杂转义。示例见 [HTTP API](http-api.md)。

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
    "save_dir": "/opt/colony_system/data/compensate_eval/closed_loop"
  }
}
```

或在任务中提供 `capture.save_dir`。闭环检测入口见 [任务与命令行](tasks.md)。

## 串口或相机被占用

确认没有其它进程占用同一串口或 MVS 相机。

录像期间 workflow 复用共享录像相机。非录像时，autofocus 和正式采集避免提前打开相机抢句柄。

选错相机时查 `camera.yaml` 的 `serial_number`、`ip`、`device_index`。优先级：`serial_number` > `ip` > `device_index`。

## 相机接口超时，重启 API 后暂时恢复

先同时请求 `/health` 和 `/api/camera/record/status`，再检查 `logs/api_server.log`（API 错误、服务生命周期、非任务设备诊断与相机监督器）和 `logs/camera_worker.log`（相机子进程）。请求结果摘要位于 `logs/api_access.log`，用响应头 `X-Request-ID` 对应的 `request_id=` 关联 API 和后台任务记录。任务生命周期、阶段及任务内设备诊断位于 `logs/task.log`，按 `task_id=` 关联排查。生产模式使用跨文件一致的脱敏任务标识；非任务操作标记为 `task_id=-`，任务执行中出现该值需要检查上下文传递。各文件按本地日期每日轮转，归档名为 `原文件名.YYYY-MM-DD`，保留最近 30 个自然日（含当天），初始化和轮转时清理过期归档。详细配置与验收见 [日志规范](logging.md)。

旧实现把后台录像、状态查询和停止流程串在同一进程的 SDK 锁上；若 `MV_CC_GetOneFrameTimeout`、`MV_CC_StopRecord` 或关闭调用不返回，状态请求也会排队，重启只能暂时清掉句柄和锁，不能消除根因。

当前实现的状态查询只读 API 内存，MVS 原生调用由独立相机子进程承载。出现原生超时时应看到 `error_code`、`worker_restart_count` 增加且 `worker_pid` 被替换；恢复链路后应能直接重试，无需重启 API。若 `/health` 或状态查询仍长时间无响应，或者 PID 未被替换，均按发布阻断处理并保留两份日志、请求时间线和残留的唯一 `*.part.avi`。不要在 `Waiting for application shutdown` 时连续按两次 `Ctrl+C`，否则无法验证正常 lifespan 清理。

## 配置校验失败

报错带字段路径，例如 `camera.objective_settings.10x`。按路径改 YAML，不要删校验器。用法见 [环境与配置](setup.md)。

## 检测在切镜前就失败

含 `detect` 的任务会在运动前做重叠去重预检；默认 4x 模型还会额外检查相机和权重：

- 所有检测入口：`scan.overlap > 0`（缺省 `0`）时必须 `deduplication.calibrated=true`，并显式给出 `registration_tolerance_mm`
- 默认 4x 模型：`objective_name` 必须是 `4x`
- 默认 4x 模型：分辨率 5120×5120，且 `allow_downscale=false`
- 默认 4x 模型：`detect.model_dir` 必填
- 默认 4x 模型：`provider=cuda` 且不允许静默落到 CPU

若任务已拍完图、推理也跑完，最后才报 `overlapping-view deduplication requires a calibrated registration threshold`，说明当时用的规则/第三方入口跳过了模型预检。在 `detect` 中补上标定字段后重发；部署含该预检修复的版本后，同类请求会在切镜前失败。

契约见 [视觉检测](vision.md)。

## 维护习惯

- 改 `.py` → 重启 uvicorn → 再发任务
- 改 YAML → `python -m workflow.config_validator`
- 改相机、视觉、handoff、对焦 → 在开发支线跑完整测试，部署前再做配置校验和硬件分阶段验收
- 联调保留请求 JSON、`scan_result.json`、`detect_result.json`、`result.json`
- 标定文件建议进版本库；现场私有参数可用单独文件覆盖
- 功能 commit 不要混入大量 `data/` 运行产物
