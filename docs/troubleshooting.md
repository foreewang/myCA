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
- `motor.move_timeout_s` 是否仍为生产基线 30 秒；缺失、非有限数、`<= 0` 或 `> 120` 会被配置校验拒绝

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

## 相机接口超时，重启 API 后暂时恢复

先同时请求 `/health` 和 `/api/camera/record/status`，再检查 `logs/api_server.log` 与 `logs/camera_worker.log`。旧实现把后台录像、状态查询和停止流程串在同一进程的 SDK 锁上；若 `MV_CC_GetOneFrameTimeout`、`MV_CC_StopRecord` 或关闭调用不返回，状态请求也会排队，重启只能暂时清掉句柄和锁，不能消除根因。

当前实现的状态查询只读 API 内存，MVS 原生调用由独立相机子进程承载。出现原生超时时应看到 `error_code`、`worker_restart_count` 增加且 `worker_pid` 被替换；恢复链路后应能直接重试，无需重启 API。若 `/health` 或状态查询仍长时间无响应，或者 PID 未被替换，均按发布阻断处理并保留两份日志、请求时间线和残留的唯一 `*.part.avi`。不要在 `Waiting for application shutdown` 时连续按两次 `Ctrl+C`，否则无法验证正常 lifespan 清理。

正常停止只按一次 `Ctrl+C`，给 Uvicorn 和现场服务管理器至少 90 秒总停止宽限。停机第一阶段会先关闭全局准入，并向任务、往复位移台和相机同时发布停止信号；第二阶段才等待任务记录清理、运动线程退出、录像提交和相机子进程退出。若其中任一环节未静默，程序会继续持有 generation lock，并保留/转化持久安全 marker；不要删除固定的 `data/api_server.lock`、`data/camera_worker.lock` 或强启第二实例。

## `/api/hardware/status` 返回 `safe=false`

这表示 XY、物镜或自动对焦停止路径至少有一次控制字写入失败或抛异常，或者相机子进程在终止、强杀后仍无法确认退出。接口同时返回 `busy=true`，`safety_latch.error_code/message/latched_at` 记录首次故障；新的硬件请求应返回 503 `HARDWARE_SAFETY_LATCHED`。相机终止未确认时还应在录像状态中看到 `error_code=CAMERA_WORKER_TERMINATION_UNCONFIRMED`、`worker_termination_pending=true`，此时禁止启动位移台、物镜、自动对焦或新的录像任务。

不要通过改内存、删除 `data/hardware_safety_latch.json`/`.lock` 文件或立即重启来“清错”。锁存和异常退出的 `generation_active` 标记会跨重启保持禁止准入。先停止上位机流量；有人身或设备风险时按物理急停；保存 API 日志、任务 ID、轴/从站、驱动器报警码、状态字和位置；再完整停止 API 与全部相机子进程。只有确认所有机构已经物理停稳、串口/相机无残留占用并完成故障处置后，才可执行离线恢复：

```powershell
python tools/clear_hardware_safety_latch.py `
  --operator "工号或姓名" `
  --reason "现场检查和故障处置记录" `
  --confirm "I CONFIRM ALL HARDWARE IS PHYSICALLY STOPPED"
```

必须在与 API 服务相同的环境变量下运行该命令。它不接受 latch 路径，且只有同时拿到 API generation lock 和相机 worker lock 才会把固定 marker 原样归档并生成 clearance receipt；任一锁仍被占用都会拒绝清除。复机前核对命令返回的 `archive_path`、`receipt_path` 与 receipt 的 `status=cleared`。项目没有也不应增加 HTTP 清锁接口。

还要注意：当前 `quick_stop()` 实际写的是 Shutdown 控制字 `0x0006`，返回成功只说明 Modbus 写入成功，并未轮询状态字或确认零速度。若现场手册映射未确认、代码尚未实现有界状态/停稳确认，或 X/Y/物镜/调焦轴故障注入没有归档证据，即使 `safe=true` 也必须按上线硬阻断处理，不能宣称问题彻底解决。

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
