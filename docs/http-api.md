# HTTP API

完整接口审查、无硬件负例和真实硬件分阶段测试见 [HTTP 功能接口审查与真实硬件联调测试方案](http-api-test-plan.md)。

## 启动

生产必须单 worker。系统只控制一套相机和电机；`hardware_guard` 是进程内锁，多 worker 互不相认。

```powershell
uvicorn workflow.api_server:app --host 0.0.0.0 --port 8000 --workers 1
```

若 `COLONY_API_WORKERS`、`UVICORN_WORKERS` 或 `WEB_CONCURRENCY` 显式大于 1，服务拒绝启动。同时持有 `data/api_server.lock`，阻止第二个 API 进程。

只改 Python、无硬件时可用 `--reload`。联调和生产不要用：reload 会杀进程，可能打断相机和串口。不要 `--workers 2` 或更高。

```powershell
uvicorn workflow.api_server:app --host 0.0.0.0 --port 8000 --reload
```

## 队列与取消

普通任务由一个后台线程串行执行，队列最多 16 个待执行项。

`POST /api/tasks/execute` 返回 202 只表示已入队，不表示硬件已动。

| status | 含义 |
| --- | --- |
| `queued` | 已受理，等待执行 |
| `running` | 执行中 |
| `success` | 成功 |
| `failed` | 失败 |
| `canceled` | 在安全检查点响应了取消 |
| `interrupted` | 服务异常退出时发现任务未正常结束 |

`POST /api/tasks/{task_id}/cancel` 只设取消标记。任务在下一检查点停下，不保证请求返回时硬件已停。启动时会把遗留的 `queued` / `running` 标成 `interrupted`。

## 路径边界

HTTP 里配置路径只能在项目 `config/`。任务输入输出只能在 `data/` 或 `outputs/`。相对路径按项目根解析。CLI 不走这层检查，但仍应写在项目目录内。

## 日志

默认文件：

```text
C:/colony_system/logs/api_server.log
```

内网调试可留详细路径和任务 ID。生产建议脱敏：

```powershell
$env:COLONY_LOG_REDACT_SENSITIVE="1"
```

开启后类似：

```text
task_id=<task:8f3a21c9> path=<DATA_ROOT>/<redacted>
```

| 环境变量 | 默认 | 作用 |
| --- | --- | --- |
| `COLONY_LOG_REDACT_SENSITIVE` | `0` | 总开关 |
| `COLONY_LOG_REDACT_PATHS` | `1` | 脱敏路径 |
| `COLONY_LOG_REDACT_TASK_ID` | `1` | 脱敏任务 ID |
| `COLONY_FILE_IO_SLOW_WARNING_MS` | `500` | 读写重试超过该毫秒记 warning |

任务索引和结果 JSON 使用原子写入和短暂重试。Windows 文件锁、杀毒或高频轮询造成阻塞时会出现：

```text
FILE_IO_SLOW: op=read_json path_kind=task_record ... elapsed_ms=1050.3 attempts=22
```

这表示最终可能已成功，只是慢。不要先缩短重试；先降轮询频率或查杀毒。

## 任务接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/health` | 健康检查 |
| `GET` | `/api/hardware/status` | 任务、录像、位移台是否占用硬件 |
| `POST` | `/api/tasks/execute` | 异步提交，202 |
| `POST` | `/api/tasks/{task_id}/cancel` | 请求取消 |
| `GET` | `/api/tasks/{task_id}/status` | 状态、进度、当前阶段 |
| `GET` | `/api/tasks/{task_id}/result` | 结果；运行中返回进度摘要 |
| `GET` | `/api/tasks/{task_id}/wells/{well_name}/images` | 孔位图片分页，`limit/offset` 或 `page/page_size` |
| `GET` | `/api/tasks/{task_id}/wells/{well_name}/images/{filename}` | 下载图片 |
| `POST` | `/api/stage/reciprocation/start` | 启动固定孔位往复，202 |
| `POST` | `/api/stage/reciprocation/stop` | 请求停止往复 |
| `GET` | `/api/stage/reciprocation/status` | 往复状态和位置 |

字段含义见 [任务与命令行](tasks.md)。含检测的请求体示例：

```json
{
  "task": {
    "task_id": "pipeline_C5_detect_http_001",
    "task_type": "pipeline",
    "stages": ["capture", "detect"],
    "plate_type": "24-well",
    "objective_name": "4x",
    "observe_scope": "well_list",
    "target": { "well_list": ["C5"] },
    "capture": {
      "save_dir": "C:/colony_system/data/http_tests/pipeline_C5_detect_http_001",
      "filename_pattern": "{well}_{index:03d}_row{row:02d}_col{col:02d}.bmp"
    },
    "motion": {
      "port": "COM3",
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
      "settle_s": 0.8
    },
    "detect": {
      "entrypoint": "vision.vision.instance_pipeline:process_image",
      "model_dir": "C:/models/ipsc_4x/2026-08-validated",
      "provider": "cuda",
      "allow_cpu_fallback": false,
      "deduplication": {
        "calibrated": true,
        "registration_tolerance_mm": 0.10,
        "intersection_over_min_threshold": 0.50
      },
      "save_overlay": true,
      "overlay_source": "vision"
    },
    "output": {
      "result_json": "C:/colony_system/data/http_tests/pipeline_C5_detect_http_001/result.json"
    }
  },
  "persist_result": true
}
```

PowerShell 发 JSON 用 `ConvertTo-Json` 和 `Invoke-RestMethod`，不要手写复杂转义。

## 录像

| 方法 | 路径 |
| --- | --- |
| `POST` | `/api/camera/record/start` |
| `GET` | `/api/camera/record/status` |
| `POST` | `/api/camera/record/stop` |

```powershell
$body = @{
  save_path = "C:/colony_system/data/camera_records/test_record.avi"
  camera_path = "C:/colony_system/config/camera.yaml"
  serial_number = "DA8583237"
  ip = "192.168.0.66"
  pixel_format = "mono8"
  fps = 10
  bitrate_kbps = 1000
} | ConvertTo-Json

Invoke-RestMethod `
  -Uri "http://127.0.0.1:8000/api/camera/record/start" `
  -Method Post `
  -ContentType "application/json" `
  -Body $body
```

- 录像期间仍可提交 `capture` / `pipeline`。拍照复用录像相机对象，用录像线程的帧做快照。
- 启动前会校验 `camera.yaml`。请求体里的 `mvs_python_dir`、`serial_number`、`ip`、`device_index`、`pixel_format` 会覆盖配置并再校验。
- 先写同目录 `.part.avi`，停止成功后再换成 `.avi`。图片接口会拒绝未完成的 `.part`。

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/camera/record/stop" -Method Post
```

## 位移台往复

不是任意两点运动。固定读 `config/plates.yaml` 的 `24-well`，走 `B2`–`B4`、`C2`–`C4`，并用同一套 `stage_limits` 做预检和运行中监控。一轮结束后从头循环；省略 `max_cycles` 则一直跑到 stop。

请求体不能覆盖 A/B 点或限位。旧字段 `point_a_*`、`point_b_*`、`limit_check_enabled`、`x_min` 等会 422。改路径或限位请改并校验 `plates.yaml`。

仅用于已完成坐标和限位标定的设备：

```powershell
$body = @{
  port = "COM3"
  baudrate = 115200
  x_slave = 1
  y_slave = 2
  profile_vel = 800000
  profile_acc = 800000
  profile_dec = 800000
  arrival_tolerance = 80
  move_timeout_s = 120
  max_cycles = 1
} | ConvertTo-Json

Invoke-RestMethod `
  -Uri "http://127.0.0.1:8000/api/stage/reciprocation/start" `
  -Method Post `
  -ContentType "application/json" `
  -Body $body
```

新电机首次联调不要用该接口。任意标定两点的精度脚本见 [硬件与安全](hardware.md)。
