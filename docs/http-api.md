# HTTP API 后端联调契约与请求示例

> 核对基线：2026-09-01，当前工作区源码，FastAPI 应用版本 `0.4.0`。
>
> 本文直接以 `workflow/api_server.py`、`workflow/api_models.py` 和实际执行器为准，不从旧 Markdown 示例反推字段。项目自定义路由共 14 个；FastAPI 自动文档路由另列在文末。真实硬件必须按 [硬件与安全](hardware.md) 分阶段验收。

## 1. 联调前必须确认

- 基础地址示例：`http://127.0.0.1:8000`。
- JSON 接口使用 `Content-Type: application/json`。
- 当前源码没有认证、授权、TLS 或 CORS 中间件。只能部署在受控内网；跨域浏览器调用需要由网关配置 CORS，公网暴露前必须补认证和 TLS。
- API 服务只能启动一个 worker。MVS SDK 与相机句柄只存在于该 API worker 管理的独立相机子进程中；原生调用超时后会终止并重建子进程，不需要重启 API。
- `POST /api/tasks/execute` 的 `202` 只表示入队成功，不代表业务字段、模型、配置或硬件检查通过。调用方必须轮询任务到终态。
- 录像 `start`/`stop` 同样返回 `202`，只表示启停请求已受理；必须轮询录像状态。`cancel`、录像 `stop` 和往复 `stop` 都不是硬件急停；运动设备旁仍需可用的物理急停。

启动命令：

```bash
uvicorn workflow.api_server:app --host 0.0.0.0 --port 8000 --workers 1
```

若 `COLONY_API_WORKERS`、`UVICORN_WORKERS` 或 `WEB_CONCURRENCY` 显式大于 1，服务会拒绝启动。服务还会持有 `data/api_server.lock`，阻止第二个 API 进程。

## 2. 全部项目接口清单

| # | 方法 | 路径 | 请求体 | 成功状态 | 功能 |
| ---: | --- | --- | --- | ---: | --- |
| 1 | `GET` | `/health` | 无 | 200 | HTTP 进程存活检查 |
| 2 | `GET` | `/api/hardware/status` | 无 | 200 | 查询进程内硬件占用者 |
| 3 | `GET` | `/api/camera/record/status` | 无 | 200 | 查询后台录像状态 |
| 4 | `POST` | `/api/camera/record/start` | 必须为 JSON 对象，可传 `{}` | 202 | 受理后台录像启动 |
| 5 | `POST` | `/api/camera/record/stop` | 可省略、`null` 或 `{}` | 202 | 受理停止和 AVI 提交 |
| 6 | `POST` | `/api/stage/reciprocation/start` | 可省略、`null` 或 JSON 对象 | 202 | 启动固定六孔位往复 |
| 7 | `POST` | `/api/stage/reciprocation/stop` | 可省略、`null` 或 JSON 对象 | 200 | 协作式停止往复 |
| 8 | `GET` | `/api/stage/reciprocation/status` | 无 | 200 | 查询往复状态 |
| 9 | `POST` | `/api/tasks/execute` | 必须为 JSON 对象 | 202 | 异步提交普通任务 |
| 10 | `POST` | `/api/tasks/{task_id}/cancel` | 无 | 200 | 请求取消任务 |
| 11 | `GET` | `/api/tasks/{task_id}/status` | 无 | 200 | 查询任务状态和进度 |
| 12 | `GET` | `/api/tasks/{task_id}/result` | 无 | 200 | 查询运行摘要或终态结果 |
| 13 | `GET` | `/api/tasks/{task_id}/wells/{well_name}/images` | 无；有查询参数 | 200 | 分页列出孔位图片 |
| 14 | `GET` | `/api/tasks/{task_id}/wells/{well_name}/images/{filename}` | 无 | 200 | 下载孔位目录内文件 |

“无请求体”表示不要发送 JSON；路径参数和查询参数仍按下面各节传递。

## 3. 通用约定

### 3.1 错误响应

公开错误统一为：

```json
{
  "detail": {
    "error_code": "ERROR_CODE",
    "message": "公开错误信息"
  }
}
```

常见 HTTP 状态：

| HTTP | 含义 |
| ---: | --- |
| 400 | 路径越界、任务 ID 为空、配置参数不合法 |
| 404 | 任务、孔位、图片目录或文件不存在 |
| 409 | 硬件占用、录像或往复状态冲突 |
| 422 | FastAPI/Pydantic 字段类型、范围或请求体校验失败 |
| 429 | 普通任务等待队列已满 |
| 503 | 任务运行器未启动，或记录/结果文件暂时不可读 |
| 500 | 后台任务执行异常、结果异常或未处理异常 |

任务业务错误通常不会直接作为提交请求的 4xx 返回，而是先返回 202，随后任务状态变为 `failed`，并在状态响应中给出 `error_code` 和 `error`。

### 3.2 路径边界

相对路径按项目根目录 `/opt/colony_system` 解析。HTTP 层会把下列路径规范化成绝对路径：

| 允许目录 | 字段 |
| --- | --- |
| 只能位于 `config/` | 外层 `camera_path`、`objectives_path`、`plates_path`；录像 `camera_path` |
| 只能位于 `data/` 或 `outputs/` | 外层 `dump_json`、录像 `save_path`；`task.capture.save_dir`、`task.scan.output_json`、`task.detect.output_json`、`task.detect.input_scan_result_json`、`task.compensate.input_detect_json`、`task.compensate.output_json`、`task.compensate.closed_loop.save_dir`、`task.output.result_json/scan_json/detect_json/compensate_json` |

`detect.model_dir` 是模型包目录，`mvs_python_dir` 是本机 SDK 目录；二者不走上述输出路径白名单，但会在各自运行流程中校验。不要允许不可信客户端任意填写这些本机路径。

### 3.3 未知字段处理

| 位置 | 当前行为 |
| --- | --- |
| 录像启停、往复启停、普通任务外层 | 未知字段直接返回 422（`extra="forbid"`） |
| `task` 内部 | OpenAPI 只声明为任意 JSON 对象，很多业务字段在后台线程中才检查 |

严格校验只覆盖各接口声明的外层模型；`task` 内部仍由后台业务校验。因此，后端不能把“Swagger 提交成功”当成任务业务结构正确。

### 3.4 任务 ID

推荐只使用 `[A-Za-z0-9_.-]`，并保证全局唯一，例如 `pipeline_C3_20260827_001`。源码会把其他字符替换为 `_` 作为索引文件名，可能造成碰撞。活动任务 ID 重复提交返回 409；终态任务 ID 可以再次提交并覆盖任务索引，因此生产端仍应每次生成新 ID。

## 4. 健康与占用查询

### 4.1 `GET /health`

请求体：无。

```http
GET /health HTTP/1.1
Host: 127.0.0.1:8000
```

响应示例：

```json
{
  "status": "ok"
}
```

该接口只证明 HTTP 进程可响应，不检查相机、串口、电机或模型。

### 4.2 `GET /api/hardware/status`

请求体：无。

```http
GET /api/hardware/status HTTP/1.1
Host: 127.0.0.1:8000
```

空闲响应示例：

```json
{
  "busy": false,
  "owners": [],
  "owner": {}
}
```

`owners` 可能同时列出一个普通硬件操作和一个 `camera_record`。当前实现允许进入稳定 `recording` 状态的录像相机被兼容采集任务复用；`starting/stopping` 期间新任务会返回 409 `CAMERA_RECORD_TRANSITION`。若兼容任务已经在运行，停止录像会返回 409 `CAMERA_RECORD_STOP_BLOCKED`，必须先等待或取消该任务，避免中途关闭共享相机会话。该状态仍只是进程内软件锁，不是硬件在线探测。

## 5. 相机录像接口

### 5.1 `GET /api/camera/record/status`

请求体：无。

```http
GET /api/camera/record/status HTTP/1.1
Host: 127.0.0.1:8000
```

未录像时：

```json
{
  "state": "idle",
  "recording": false,
  "background": false,
  "starting": false,
  "stopping": false,
  "opened": false,
  "sdk_hung": false,
  "operation_id": null,
  "session_id": null,
  "worker_pid": 18120,
  "worker_restart_count": 1,
  "error": null,
  "error_code": null,
  "settings": {},
  "last_video": {}
}
```
录像时：
```json
{
    "state": "recording",
    "recording": true,
    "background": true,
    "starting": false,
    "stopping": false,
    "opened": true,
    "sdk_hung": false,
    "operation_id": "2fcfd0b1c10948cdb3c99d801d60dc27",
    "session_id": "ad8cab0a98f9414b98890715f86a24c5",
    "worker_pid": 18120,
    "worker_restart_count": 1,
    "last_frame_progress_at": 1788232105.482,
    "saved_path": "/opt/colony_system/data/camera_records/recording.avi",
    "frame_rate": 10.0,
    "bitrate_kbps": 1000,
    "frame_count": 164,
    "duration_s": 45.87022662162781,
    "error": null,
    "error_code": null,
    "settings": {
        "save_path": "/opt/colony_system/data/camera_records/recording.avi",
        "mvs_python_dir": "/opt/MVS/Samples/64/Python/MvImport",
        "device_index": 0,
        "serial_number": "DA8583237",
        "camera_ip": "192.168.0.66",
        "pixel_format": "mono8",
        "exposure_us": 5000,
        "gain": 0.0,
        "fps": 10.0,
        "bitrate_kbps": 1000,
        "timeout_ms": 5000
    }
}
```
`state` 是权威状态，取值包括 `idle/opening/open/starting/recording/stopping/closing/recovering/faulted`。`starting` / `stopping` 期间不要再开另一路相机；状态查询只读取 API 进程内存，不调用 MVS SDK，因此即使原生调用卡住仍应快速响应。

若子进程超时、异常退出、录像线程失败或关闭不完整，监督器会隔离旧 PID 并尝试创建新 PID。恢复成功后 `state=idle`、`worker_restart_count` 增加，`error/error_code` 保留最近故障供诊断；此时可直接重新启动录像，无需重启 API。只有 `state=faulted` 表示自动重建失败，下一次相机操作会再次尝试恢复。
### 5.2 `POST /api/camera/record/start`

请求体本身必传，但所有字段都有默认值；最小合法请求体是：

```json
{}
```

请求对象包含任何未声明字段（包括字段名拼写错误）时返回 422，不会启动相机操作。

联调建议显式请求体：

```json
{
  "save_path": "data/camera_records/backend_joint_001.avi",
  "camera_path": "config/camera.yaml",
  "device_index": 0,
  "serial_number": "DA8583237",
  "ip": "192.168.0.66",
  "mvs_python_dir": "/opt/MVS/Samples/64/Python/MvImport",
  "pixel_format": "mono8",
  "exposure_us": 5000,
  "gain": 0.0,
  "fps": 10.0,
  "bitrate_kbps": 1000,
  "timeout_ms": 5000
}
```

受理响应为 HTTP 202，通常在原生 `EnumDevices/OpenDevice/StartRecord` 完成前返回：

```json
{
  "state": "starting",
  "starting": true,
  "recording": false,
  "operation_id": "2fcfd0b1c10948cdb3c99d801d60dc27",
  "worker_pid": null,
  "saved_path": "/opt/colony_system/data/camera_records/backend_joint_001.avi",
  "error": null,
  "error_code": null
}
```

收到 202 后轮询 `/api/camera/record/status`：`state=recording` 表示启动成功；`state=idle` 且 `error` 非空表示启动失败并已完成清理/隔离；`state=faulted` 表示子进程重建仍失败。不要把 202 当作相机已经打开。

字段契约：

| 字段 | 类型 | 默认值 | Pydantic 约束/行为 |
| --- | --- | --- | --- |
| `save_path` | string | `data/camera_records/recording.avi` | 非空；必须在 `data/` 或 `outputs/` |
| `camera_path` | string/null | `config/camera.yaml` | 必须在 `config/` |
| `device_index` | integer/null | 读取 YAML | 0–63 |
| `serial_number` | string/null | 读取 YAML | 覆盖 YAML |
| `ip` | string/null | 读取 YAML | 覆盖 YAML |
| `mvs_python_dir` | string/null | 读取 YAML | 覆盖 SDK 导入目录并重新校验相机配置 |
| `pixel_format` | string/null | 读取 YAML | 覆盖 YAML；当前正式配置使用 `mono8` |
| `exposure_us` | number/null | 读取 YAML | `0 < value <= 10000000` |
| `gain` | number/null | 读取 YAML | 0–60 |
| `fps` | number/null | 10.0 | 非 null 时 `0 < value <= 240` |
| `bitrate_kbps` | integer | 1000 | 1–500000 |
| `timeout_ms` | integer/null | null | 非 null 时 `0 < value <= 15000`；须 ≥ 曝光(ms)+2000ms 传输余量。null 时按该规则自动计算，并在 status.settings 中返回实际生效值 |

每次录像使用同目录、带唯一会话标识的 `*.part.avi`，正常停止并关闭相机后通过同文件系统原子替换提交为正式 `.avi`；异常终止会保留该临时文件供现场取证，不会与下次录像冲突。同一 API worker 只能有一个相机子进程和一个后台录像。打开命令默认 20 秒超时；超时会终止旧子进程并自动重建。

服务正常退出时也会在受限时间内尝试停止活动录像并提交正式 AVI；如果原生调用超过退出预算，则终止相机子进程并保留唯一临时文件，保证 API 退出不会再次无限等待。

### 5.3 `POST /api/camera/record/stop`

推荐不发送请求体；为兼容通用 JSON 客户端，也接受 `null` 或空对象 `{}`。任何非空对象都包含未声明字段，会返回 422，且不会执行停止操作。

```http
POST /api/camera/record/stop HTTP/1.1
Host: 127.0.0.1:8000
Content-Length: 0
```

受理响应为 HTTP 202；它不等待后台线程退出、`StopRecord`、相机关闭或文件提交：
```json
{
  "status": "stopping",
  "state": "stopping",
  "stopping": true,
  "recording": false,
  "operation_id": "2fcfd0b1c10948cdb3c99d801d60dc27",
  "worker_pid": 18120,
  "saved_path": "/opt/colony_system/data/camera_records/recording.avi",
  "video": {},
  "error": null,
  "error_code": null
}
```

随后轮询状态。停止成功时返回 `state=idle`、`error=null`，并在 `last_video` 中给出已提交的正式文件及帧数、时长等元数据。只有临时文件存在、非空且 `frame_count > 0` 才会提交正式 AVI。停止失败时同样回到可恢复的 `idle`，但 `error/error_code` 非空，未提交的唯一 `*.part.avi` 会保留；旧相机子进程会被终止并重建。停止命令默认总超时 75 秒，`StopRecord` 和关闭操作还有各自的内部上限，因此 API 进程与状态接口不会被无限卡住。

没有活动录像时不是幂等成功，而是立即返回 409 `CAMERA_RECORD_STOP_FAILED`。兼容任务正在复用录像相机时返回 409 `CAMERA_RECORD_STOP_BLOCKED`。不要在 `stopping` 状态重复发送 stop，也不要在看到 HTTP 202 后立即读取正式 AVI；以 `state=idle + error=null + last_video.saved_path + last_video.frame_count>0` 作为完成条件。

### 5.4 相机子进程超时与上线监控

以下环境变量均为秒，必须是有限数字且落在源码允许范围内；非法值会让服务启动失败，而不是静默采用错误配置：

| 环境变量 | 默认值 | 作用 |
| --- | ---: | --- |
| `COLONY_CAMERA_WORKER_STARTUP_TIMEOUT_S` | 8 | 子进程启动握手 |
| `COLONY_CAMERA_OPEN_TIMEOUT_S` | 20 | 打开相机/启动录像命令 |
| `COLONY_CAMERA_STOP_TIMEOUT_S` | 75 | 停止、关闭及文件元数据返回总时限 |
| `COLONY_CAMERA_CLOSE_TIMEOUT_S` | 45 | 普通拍照/对焦会话关闭 |
| `COLONY_CAMERA_CAPTURE_SLACK_S` | 5 | 单帧取帧超时之外的进程通信余量 |
| `COLONY_CAMERA_TERMINATE_GRACE_S` | 2 | terminate 后升级为 kill 前的等待 |
| `COLONY_CAMERA_MONITOR_INTERVAL_S` | 0.5 | 录像子进程健康轮询间隔 |
| `COLONY_CAMERA_MONITOR_TIMEOUT_S` | 2 | 单次健康轮询时限 |
| `COLONY_CAMERA_RECORD_STALL_MIN_S` | 20 | 录像帧计数无进展的最小容忍时长 |
| `COLONY_CAMERA_RECORD_STALL_GRACE_S` | 5 | 取帧超时之外的停滞判定余量 |

API 错误、服务生命周期、非任务设备诊断及监督器超时、隔离和 PID 重建记录在 `logs/api_server.log`。`workflow.access` 为每个请求输出一条结果摘要到 `logs/api_access.log`：`event=request_completed`、方法、路由模板、状态码和耗时，不记录查询串或请求正文，未匹配路由记为 `<unmatched>`。响应头 `X-Request-ID` 返回服务生成的请求标识，错误日志和后台任务沿用该标识；Uvicorn 原生访问记录关闭，避免重复输出。

任务入队、开始、阶段变化、完成、取消和失败，以及任务内设备诊断写入 `logs/task.log`。每条日志包含带时区时间、级别、logger、`pid`、`request_id` 和 `task_id`；任务阶段另外包含 `stage`、`well`，终态包含耗时或错误原因。非任务操作使用 `task_id=-`，正常任务执行记录出现 `-` 应作为上下文缺失排查。三个文件各由一个共享 handler 轮转，按本地日期每日轮转，归档带日期后缀，保留最近 30 个自然日（含当天）。完整职责、脱敏和验收方法见 [日志规范](logging.md)。

子进程内的 MVS/controller 阶段日志单独轮转到 `logs/camera_worker.log`（同样每日轮转、保留最近 30 个自然日，含当天）。可用 `COLONY_CAMERA_WORKER_LOG_LEVEL` 调整子进程日志级别，用 `COLONY_CAMERA_WORKER_LOG_PATH` 覆盖文件路径；显式设为空字符串可关闭子进程文件日志。

生产监控至少告警以下条件：`state=faulted`；终态 `error` 非空；`worker_restart_count` 增加；同目录持续积累唯一 `*.part.avi`。单次重建成功后系统可继续服务，但重建次数增长通常说明相机链路、MVS 驱动、网卡或设备供电仍不稳定，不能只靠自动重试掩盖。

真实硬件上线前必须连续验证：启停和录像中快照；启动/停止阶段高频查询状态；拔网线或模拟 SDK 卡死后的 PID 替换和再次启动；录像时执行兼容采集及自动对焦；服务退出时无异常 `CancelledError` 或 lifespan 报错；正式 AVI 可播放且帧数、时长合理。软件故障注入测试不能替代这组现场验收。

正常链路可先运行 20 轮自动验收；脚本会校验 202 契约、状态延迟、终态、视频元数据和非预期 PID 重建：

```bash
python tools/camera_record_soak.py --cycles 20 --record-seconds 10 --serial-number DA8583237
```

## 6. 位移台固定往复接口

该功能不是任意两点运动。源码固定读取 `config/plates.yaml` 的 `24-well`，依次走 `B2`、`B3`、`B4`、`C2`、`C3`、`C4`，并使用板型的 `stage_limits` 做预检和运行时保护。请求不能覆盖孔位、坐标或限位。

### 6.1 `POST /api/stage/reciprocation/start`

请求体可以完全省略、传 `null` 或传 `{}`。这三种方式都会采用默认速度并持续循环，真实设备联调不应这样调用。

真实硬件的显式单周期示例：

```json
{
  "port": "/dev/ttyUSB0",
  "baudrate": 115200,
  "x_slave": 1,
  "y_slave": 2,
  "profile_vel": 100000,
  "profile_acc": 100000,
  "profile_dec": 100000,
  "arrival_tolerance": 80,
  "poll_s": 0.05,
  "settle_s": 0.2,
  "move_timeout_s": 120.0,
  "max_cycles": 1
}
```

字段契约：

| 字段 | 类型 | 默认值 | 范围 |
| --- | --- | ---: | --- |
| `port` | string | `/dev/ttyUSB0` | 长度 1–64 |
| `baudrate` | integer | 115200 | 1200–921600 |
| `x_slave` | integer | 1 | 1–247 |
| `y_slave` | integer | 2 | 1–247 |
| `profile_vel` | integer | 800000 | 1–10000000 |
| `profile_acc` | integer | 800000 | 1–10000000 |
| `profile_dec` | integer | 800000 | 1–10000000 |
| `arrival_tolerance` | integer | 80 | 0–1000000 pulse |
| `poll_s` | number | 0.05 | `0 < value <= 10` 秒 |
| `settle_s` | number | 0.2 | 0–60 秒 |
| `move_timeout_s` | number | 120.0 | `0 < value <= 3600` 秒 |
| `max_cycles` | integer/null | null | 1–1000000；null 表示运行到 stop |

旧字段 `point_a_*`、`point_b_*`、`limit_check_enabled`、`x_min/x_max`、`y_min/y_max`、`safety_margin` 以及其他未知字段都会返回 422。

### 6.2 `POST /api/stage/reciprocation/stop`

请求体可省略或传 `null`，默认最多等待后台线程 5 秒：

```http
POST /api/stage/reciprocation/stop HTTP/1.1
Host: 127.0.0.1:8000
Content-Length: 0
```

需要覆盖等待时间时：

```json
{
  "join_timeout_s": 10.0
}
```

`join_timeout_s` 范围为 0–120 秒。响应 `status="stopping"` 表示只完成了停止请求，线程仍未退出；应继续查询状态，直到 `stopped` 或 `failed`。没有活动往复时调用会返回 `stopped`，可作幂等清理。

除 `join_timeout_s` 外的任何字段都会返回 422，且不会执行停止操作。

### 6.3 `GET /api/stage/reciprocation/status`

请求体：无。

```http
GET /api/stage/reciprocation/status HTTP/1.1
Host: 127.0.0.1:8000
```

常见状态：`starting`、`running`、`moving`、`stopping`、`stopped`、`failed`。运动中还可能返回 `cycle`、`target`、`next_target`、`current_pos` 和 `last_move`。

## 7. 普通任务提交

### 7.1 `POST /api/tasks/execute` 外层请求

完整外层结构：

```json
{
  "task": {},
  "camera_path": "config/camera.yaml",
  "objectives_path": "config/objectives.yaml",
  "plates_path": "config/plates.yaml",
  "dump_json": "data/interface_tasks/result_override.json",
  "persist_result": true
}
```

| 字段 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `task` | 是 | 无 | 任意 JSON 对象；业务结构在后台校验 |
| `camera_path` | 否 | `config/camera.yaml` | 覆盖相机配置，只能位于 `config/` |
| `objectives_path` | 否 | `config/objectives.yaml` | 覆盖物镜配置，只能位于 `config/` |
| `plates_path` | 否 | `config/plates.yaml` | 覆盖板型配置，只能位于 `config/` |
| `dump_json` | 否 | null | 覆盖 `task.output.result_json`，只能位于 `data/` 或 `outputs/` |
| `persist_result` | 否 | true | 是否另行写总结果 JSON；任务索引仍会保存内嵌结果和状态 |

推荐只传确实需要覆盖的外层字段。`handoff` 的配置固定由本机 `config/handoff.yaml` 读取，HTTP 外层没有 `handoff_path` 字段。

外层对象除表中六个字段外出现任何未知字段都会同步返回 422；这一规则不改变 `task` 内部仍由后台校验的现有契约。

提交成功响应示例：

```json
{
  "task_id": "capture_A1_20260827_001",
  "status": "accepted",
  "task_type": "capture",
  "observe_scope": "single_well",
  "objective_name": "4x",
  "message": "task accepted",
  "result_json_path": "/opt/colony_system/data/interface_tasks/capture_A1_20260827_001/result.json"
}
```

### 7.2 `task` 公共字段

| 字段 | 适用任务 | 约定 |
| --- | --- | --- |
| `task_id` | 全部 | 必填；提交阶段唯一同步检查的业务字段 |
| `task_type` | 全部 | `capture`、`pipeline`、`compensate`、`handoff` |
| `plate_type` | 全部 | 必须存在于 `plates.yaml`，如 `24-well` |
| `objective_name` | 非 handoff | 必须存在于 `objectives.yaml`，如 `4x`、`10x`；旧别名 `objective` 会被规范化，但新请求不要再用 |
| `observe_scope` | capture/pipeline | `single_well`、`well_list`、`full_plate` |
| `target.well_name` | single_well/独立 compensate | 目标孔，如 `A1` |
| `target.well_list` | well_list | 非空孔位数组 |
| `stages` | pipeline | 推荐组合见下表；省略时 pipeline 也只执行 capture |
| `output` | 全部 | 任务结果路径和兼容别名 |

`stages` 当前按“是否包含”判断，并固定按 capture → detect → compensate 执行：

| 功能 | `stages` |
| --- | --- |
| 仅采集 | `["capture"]`，也可在 capture/pipeline 中省略 |
| 采集后检测 | `["capture", "detect"]` |
| 采集、检测、补偿 | `["capture", "detect", "compensate"]` |

当前 pipeline 不支持 detect-only；缺少 capture 会在后台失败，compensate 又必须依赖 detect。

### 7.3 采集、运动和扫描字段

| 字段 | 默认/约束 | 说明 |
| --- | --- | --- |
| `capture.save_dir` | 采集时必填 | 单孔为图片目录；多孔为基础目录，实际写入 `<save_dir>/<well>/images/` |
| `capture.filename_pattern` | 采集时必填 | 支持 `task_id/well/index/row/col/vdown/vright/x/y` 格式化字段 |
| `motion.port` | `/dev/ttyUSB0` | XY Modbus 串口 |
| `motion.baudrate` | 115200 | 波特率 |
| `motion.x_slave/y_slave` | 1 / 2 | XY 从站 |
| `motion.profile_vel/acc/dec` | 采集和补偿时必填 | 运行时要求正整数 |
| `motion.timeout_s` | 120.0 | 单次 XY 移动超时，必须大于 0 |
| `motion.poll_s` | 0.05 | 位置轮询间隔，必须大于 0 |
| `motion.settle_s` | 主要用于 handoff | 普通采集和补偿当前都使用 `scan.settle_s`（默认 0.8） |
| `motion.arrival_tolerance_pulse` | handoff 可覆盖 | 非负 pulse；普通扫描到位保护主要读取板型 `runtime_guard` |
| `scan.overlap` | 采集时必填，`0 <= value < 1` | 扫描视野重叠率 |
| `scan.use_objective_fov` | true | 从物镜配置读取 FOV |
| `scan.fov_override_mm` | 无 | `use_objective_fov=false` 时必填，可为数值或 `{width,height}` |
| `scan.settle_s` | 0.8 | 到点后稳定时间 |
| `scan.output_json` | null | 单孔扫描结果路径；多孔会按孔位派生 |

正常后端请求不需要传 `task.autofocus`，服务读取本机 `config/autofocus.yaml`。源码仍接受该字段作为本地调试/兼容覆盖，常用键为 `enabled`、`force` 和 `trigger`；生产端应把自动对焦策略留在本机受控配置中。

### 7.4 单孔采集完整示例

该请求会扫描完整培养孔，不是只拍一张图，并且可能触发物镜切换和自动对焦。

```json
{
  "task": {
    "task_id": "capture_A1_20260827_001",
    "task_type": "capture",
    "plate_type": "24-well",
    "objective_name": "4x",
    "observe_scope": "single_well",
    "target": {
      "well_name": "A1"
    },
    "capture": {
      "save_dir": "data/interface_tasks/capture_A1_20260827_001/images",
      "filename_pattern": "{well}_{index:03d}_row{row:02d}_col{col:02d}.bmp"
    },
    "motion": {
      "port": "/dev/ttyUSB0",
      "baudrate": 115200,
      "x_slave": 1,
      "y_slave": 2,
      "profile_vel": 100000,
      "profile_acc": 100000,
      "profile_dec": 100000,
      "timeout_s": 120.0,
      "poll_s": 0.05
    },
    "scan": {
      "overlap": 0.1,
      "use_objective_fov": true,
      "settle_s": 0.8,
      "output_json": "data/interface_tasks/capture_A1_20260827_001/scan_result.json"
    },
    "output": {
      "result_json": "data/interface_tasks/capture_A1_20260827_001/result.json"
    }
  },
  "persist_result": true
}
```

### 7.5 多孔采集加检测完整示例

`model_dir` 是环境占位路径。仓库当前只有 manifest 示例，没有可直接验收的生产权重；必须替换为已验证的真实模型发布目录后再发送。

```json
{
  "task": {
    "task_id": "pipeline_C3_C5_20260827_001",
    "task_type": "pipeline",
    "stages": [
      "capture",
      "detect"
    ],
    "plate_type": "24-well",
    "objective_name": "4x",
    "observe_scope": "well_list",
    "target": {
      "well_list": [
        "C3",
        "C5"
      ]
    },
    "capture": {
      "save_dir": "data/interface_tasks/pipeline_C3_C5_20260827_001",
      "filename_pattern": "{well}_{index:03d}_row{row:02d}_col{col:02d}.bmp"
    },
    "motion": {
      "port": "/dev/ttyUSB0",
      "baudrate": 115200,
      "x_slave": 1,
      "y_slave": 2,
      "profile_vel": 100000,
      "profile_acc": 100000,
      "profile_dec": 100000,
      "timeout_s": 120.0,
      "poll_s": 0.05
    },
    "scan": {
      "overlap": 0.1,
      "use_objective_fov": true,
      "settle_s": 0.8
    },
    "detect": {
      "entrypoint": "vision.vision.instance_pipeline:process_image",
      "model_dir": "C:/models/ipsc_4x/<replace-with-validated-release>",
      "provider": "cuda",
      "allow_cpu_fallback": false,
      "deduplication": {
        "calibrated": true,
        "registration_tolerance_mm": 0.1,
        "intersection_over_min_threshold": 0.5
      },
      "save_overlay": true,
      "overlay_source": "vision"
    },
    "output": {
      "result_json": "data/interface_tasks/pipeline_C3_C5_20260827_001/result.json"
    }
  },
  "persist_result": true
}
```

孔壁功能已下线，接口不再提供孔壁开关、边距参数和孔边界返回字段。`is_pickable` 保留，表示目标自身有效，不包含孔壁距离判断。模型管线的 4x 定位和 10x 复核契约不变。

内置模型检测契约：

- 只允许 `objective_name="4x"`。
- 相机必须固定为 5120×5120，且 `camera.resolution.allow_downscale=false`。
- `detect.model_dir` 必填。
- `scan.overlap > 0` 时，必须设置 `deduplication.calibrated=true`。
- `calibrated=true` 时必须显式传正数 `registration_tolerance_mm`。
- `intersection_over_min_threshold` 必须在 `(0, 1]`。
- `provider` 常用 `cuda`、`cpu`、`auto`；实际支持性由模型运行时检查。生产默认用 `cuda`，`allow_cpu_fallback` 默认 false。

检测字段中，`save_overlay` 默认 true，`overlay_source` 默认 `vision`；可选 `workflow`。工作流绘图时还会读取 `draw_bbox`、`draw_center`、`draw_image_center`。`detect.output_json` 适用于单孔；多孔任务会固定派生为 `<capture.save_dir>/<well>/detect_result.json`。

规则入口新增可选布尔字段 `task.detect.save_debug`，默认 `false`；`null`、数字和字符串均不是合法布尔值，在检测执行阶段报错，仍遵循任务 202 入队后异步报告业务错误的契约。此选项不改变 HTTP 路由、请求外层和结果结构，默认入口仍为 4x 模型。使用规则算法需显式设置以下任务片段：

```json
{
  "detect": {
    "entrypoint": "vision.vision.detect_pipeline:process_image",
    "save_debug": false,
    "save_overlay": true,
    "overlay_source": "vision",
    "deduplication": {
      "calibrated": true,
      "registration_tolerance_mm": 0.10,
      "intersection_over_min_threshold": 0.50
    }
  }
}
```

规则入口同样走跨视野唯一计数。`scan.overlap > 0`（缺省为 `0`）时必须带上 `deduplication.calibrated=true` 和显式 `registration_tolerance_mm`，否则会在预检阶段失败，避免拍完整孔后再去重报错。省略 `scan.overlap` 时不会触发这条预检；任务里若显式写成 `0.1` 且未标定，仍会在切镜前失败。

workflow 仅向上述规则 `process_image` 及 `vision.detect_pipeline:process_image` 别名转发 `save_debug`，不会注入模型或第三方入口，也不会新增其他规则算法参数的转发。有规则输出目录时，默认保留 `05_contour_mask.bmp`、`06_overlay.bmp`、`07_result.json`，识别结果与 05/06 像素保持原行为；设 `save_debug=true` 恢复全部 01–07 文件。5120×5120 的 BMP 总量约由 200 MiB 降至 100 MiB。

`save_overlay=false` 仍不向视觉入口传输出目录；`overlay_source=workflow` 仍由 workflow 绘制 overlay，规则入口不写自己的产物。`save_debug=true` 不会覆盖这两项语义，也不会让 Python 的 `out_dir=None` 开始落盘。规则 Python 入口的原默认值保持不变：`process_image` 未传 `out_dir` 时不落盘，`detect_from_gray` 默认 `out_dir=None`，`detect_from_path` 默认 `out_dir="outputs_5120_contour_refined_opt"`。同目录旧有的 01–04 不自动删除，目录中的历史调试图不代表本次生成。单图 CLI 的等价选项为 `--backend legacy --save-debug`，详见 [视觉检测](vision.md)。

### 7.6 观察范围的请求变体

以下是替换完整 capture/pipeline 示例中相应字段的 `task` 片段，不是 `/api/tasks/execute` 的独立完整请求体。

`single_well`：

```json
{
  "observe_scope": "single_well",
  "target": {
    "well_name": "A1"
  }
}
```

`well_list`：

```json
{
  "observe_scope": "well_list",
  "target": {
    "well_list": [
      "A1",
      "A2",
      "B1"
    ]
  }
}
```

`full_plate`：

```json
{
  "observe_scope": "full_plate"
}
```

`full_plate` 会按 `plates.yaml` 展开全部孔，首次硬件联调不要使用。

### 7.7 独立补偿完整示例

观察识别任务会额外导出已去重且 `is_pickable=true` 的候选文件，原始检测结果保留。
单孔可通过 `task.detect.pickable_output_json` 指定路径（仅允许 `data/` 或 `outputs/`），
默认与检测 JSON 同目录；没有检测输出路径时使用 `capture.save_dir`。
多孔固定逐孔生成 `pickable_detect_result.json`。每个原去重组只选一条可挑取观察，
保留原图片编号、克隆 ID、拍摄位置和像素偏移。空列表也会落盘。

完成任务的 `detect_result.pickable_result_json`（单孔）、孔记录的 `pickable_result_json`
及孔图片查询响应提供实际文件路径。前端可直接读取或下载：

```http
GET /api/tasks/{task_id}/wells/{well_name}/pickable-result
GET /api/tasks/{task_id}/wells/{well_name}/pickable-result?download=true
```

第一种返回 `application/json` 文件内容，第二种附带附件下载头；未记录文件或文件不存在时返回 404。
前端从 `images[].clones[]` 展示候选，选择后把代表记录的图片 `index` 和 `clone_id` 提交给补偿。
可直接将候选文件路径用作 `compensate.input_detect_json`，也可以将完整候选对象作为
`compensate.input_detect_result` 上传到现有执行接口；两种方式均使用 `selector.purpose="pick"`。
实际图片预览仍通过已有图片接口完成，JSON 中服务器路径不是浏览器 URL。
候选结构及离线转换见 [任务文档](tasks.md#可挑取且去重的候选文件)。

下面的 4x 转 10x 示例仍读取原始检测结果：只具备居中资格而 `is_pickable=false` 的模型目标
不会进入可挑取候选文件。

下面示例表示：读取已审核的 4x 定位结果，切换到 10x，并把 `eligible_for_10x_centering=true` 的指定目标移到视野中心。路径、图号和克隆 ID 必须先人工审核并替换。

```json
{
  "task": {
    "task_id": "compensate_C3_to_10x_20260827_001",
    "task_type": "compensate",
    "plate_type": "24-well",
    "objective_name": "10x",
    "observe_scope": "single_well",
    "target": {
      "well_name": "C3"
    },
    "motion": {
      "port": "/dev/ttyUSB0",
      "baudrate": 115200,
      "x_slave": 1,
      "y_slave": 2,
      "profile_vel": 100000,
      "profile_acc": 100000,
      "profile_dec": 100000,
      "timeout_s": 120.0,
      "poll_s": 0.05
    },
    "compensate": {
      "input_detect_json": "data/interface_tasks/reviewed_pipeline/C3/detect_result.json",
      "selector": {
        "mode": "image_and_clone",
        "purpose": "10x_centering",
        "image_index": 4,
        "clone_id": "C01"
      },
      "scale": {
        "x": 1.0,
        "y": 1.0
      },
      "approach": {
        "enabled": false
      },
      "closed_loop": {
        "enabled": false
      },
      "output_json": "data/interface_tasks/compensate_C3_to_10x_20260827_001/compensate_result.json"
    },
    "output": {
      "result_json": "data/interface_tasks/compensate_C3_to_10x_20260827_001/result.json"
    }
  },
  "persist_result": true
}
```

`compensate.input_detect_json` 和 `compensate.input_detect_result` 二选一；推荐服务间传受控目录中的 JSON 路径，避免提交很大的内嵌结果。

选择器契约：

| 字段/模式 | 约束 |
| --- | --- |
| `selector.purpose` | `pick` 或 `10x_centering`；默认 `pick` |
| `first` | 无附加字段 |
| `largest_area` | 无附加字段 |
| `nearest_image_center` | 无附加字段 |
| `clone_id` | `clone_id` 必填，`image_index` 可选 |
| `image_and_clone` | `image_index`、`clone_id` 都必填；推荐，避免不同图片内 clone ID 重复 |

内置 4x v2 模型会把正式定位目标标为 `is_pickable=false`，但可标为 `eligible_for_10x_centering=true`。因此 4x 结果用于转 10x 时必须显式传 `purpose="10x_centering"`；只有输入结果确有经审核的 `is_pickable=true` 目标时，才使用 `purpose="pick"`。

固定方向接近可配置 `approach.enabled`、`direction` 或 `x_direction/y_direction`（只能为 `-1` 或 `1`）、`margin_pulse` 或 `x_margin_pulse/y_margin_pulse`（非负）、`pre_settle_s`。开启闭环时可把 `closed_loop` 改为：

```json
{
  "enabled": true,
  "save_dir": "data/interface_tasks/compensate_C3_to_10x_20260827_001/closed_loop",
  "filename_pattern": "closed_loop_{task_id}_{well}_iter{iteration:02d}.bmp",
  "max_iterations": 2,
  "tolerance_px": {
    "x": 10,
    "y": 10
  },
  "detect_entrypoint": "vision.vision.detect_pipeline:process_image",
  "selector": {
    "mode": "nearest_image_center",
    "purpose": "10x_centering"
  }
}
```

闭环启用时必须有 `closed_loop.save_dir` 或 `capture.save_dir`。对 10x 闭环必须显式提供匹配 10x 的 `detect_entrypoint`；省略时会复用任务入口，而默认入口是 4x 模型。

### 7.8 pipeline 内补偿变体

在 7.5 的完整 pipeline 示例中，把 `stages` 改为：

```json
[
  "capture",
  "detect",
  "compensate"
]
```

并在 `task` 中增加：

```json
{
  "compensate": {
    "selector": {
      "mode": "nearest_image_center",
      "purpose": "10x_centering"
    },
    "scale": {
      "x": 1.0,
      "y": 1.0
    },
    "closed_loop": {
      "enabled": false
    }
  }
}
```

该变体仍在当前 4x 任务内完成中心补偿，不会自动把 `objective_name` 切成 10x；如需真正切换到 10x，使用上一节的独立补偿任务。只有候选资格、方向、比例和目标坐标均已完成真实标定后才能开启。

### 7.9 位移台开到对接点（handoff）完整示例

`task_type=handoff` **只把 XY 位移台移动到对接点位**，供自控（机械臂）放置或取走培养板。本任务不夹取、不放置培养板。`handoff.action` 只支持 `load_in` 和 `unload_out`。点位来自本机 `config/handoff.yaml`，请求不能直接覆盖对接坐标。

| `handoff.action` | 位移台做什么 | 任务成功后由自控做什么 |
| --- | --- | --- |
| `load_in` | 移动到对接点并停稳 | **放置**培养板 |
| `unload_out` | 移动到对接点并停稳 | **取走**培养板 |

软件侧：先等本任务 `success`（位移台已到位），再向自控发放板/取板指令。

```json
{
  "task": {
    "task_id": "handoff_load_in_20260827_001",
    "task_type": "handoff",
    "plate_type": "24-well",
    "handoff": {
      "action": "load_in"
    },
    "motion": {
      "port": "/dev/ttyUSB0",
      "baudrate": 115200,
      "x_slave": 1,
      "y_slave": 2,
      "profile_vel": 100000,
      "profile_acc": 100000,
      "profile_dec": 100000,
      "timeout_s": 120.0,
      "poll_s": 0.05,
      "settle_s": 0.5,
      "arrival_tolerance_pulse": 3000
    },
    "output": {
      "result_json": "data/interface_tasks/handoff_load_in_20260827_001/result.json"
    }
  },
  "persist_result": true
}
```

取板前生成新任务 ID，并把 `handoff` 改为：

```json
{
  "action": "unload_out"
}
```

`motion` 可省略部分字段并回退到 `handoff.yaml`，但联调请求建议显式保留经过审核的参数。两种动作都是“位移台先到位”；放板、取板仍由自控执行。

### 7.10 输出路径优先级与多孔目录

总结果路径优先级：外层 `dump_json` > `task.output.result_json`。`persist_result=false` 时不写总结果文件，但任务索引仍保存状态和内嵌结果。

兼容输出别名：

| 首选字段 | 兼容回退字段 |
| --- | --- |
| `scan.output_json` | `output.scan_json` 仅在部分检测结果引用中使用 |
| `detect.output_json` | `output.detect_json` |
| `compensate.output_json` | `output.compensate_json` |
| `compensate.input_detect_json` | `output.detect_json` |

当前 pipeline 始终先 capture，`detect.input_scan_result_json` 只作为结果中的扫描文件引用，不会把接口变成 detect-only 任务。

多孔目录固定派生为：

```text
data/interface_tasks/some_pipeline/
├─ C3/
│  ├─ images/
│  ├─ detect_overlays/
│  ├─ scan_result.json
│  ├─ detect_result.json
│  └─ compensate_result.json
├─ C5/
│  └─ ...
└─ result.json
```

## 8. 任务控制与结果查询

### 8.1 `POST /api/tasks/{task_id}/cancel`

请求体：无。

```http
POST /api/tasks/capture_A1_20260827_001/cancel HTTP/1.1
Host: 127.0.0.1:8000
Content-Length: 0
```

活动任务会返回 `status="cancel_requested"`；终态任务返回原终态和 `message="task is already terminal"`。取消只是设置标志，任务在下一安全检查点变为 `canceled`，请求返回时硬件可能尚未停止。

### 8.2 `GET /api/tasks/{task_id}/status`

请求体：无。

```http
GET /api/tasks/capture_A1_20260827_001/status HTTP/1.1
Host: 127.0.0.1:8000
```

状态包括：`queued`、`running`、`success`、`failed`、`canceled`、`interrupted`。关键响应字段：

| 字段 | 说明 |
| --- | --- |
| `progress` | 0–100 的总进度 |
| `current_stage` / `current_well` | 当前阶段和孔位 |
| `message` | 当前状态摘要 |
| `error_code` / `error` | 失败原因；成功时通常为 null |
| `cancel_requested` | 是否已请求取消 |
| `created_at/started_at/updated_at/finished_at` | 生命周期时间 |
| `result_json_path` | 配置的总结果路径，未必已生成 |

推荐轮询逻辑：

```text
POST execute -> 202 accepted
              -> GET status every 0.5–2 s
              -> queued/running: 继续轮询
              -> success: GET result / images
              -> failed/canceled/interrupted: 停止并记录 error_code、error
```

### 8.3 `GET /api/tasks/{task_id}/result`

请求体：无。

```http
GET /api/tasks/capture_A1_20260827_001/result HTTP/1.1
Host: 127.0.0.1:8000
```

`queued/running` 时返回带 `result: null` 的进度摘要，HTTP 仍为 200。终态时优先读取 `result_json_path` 指向的 JSON；否则返回任务索引中的内嵌结果或整条记录。调用方应先根据 `status` 判定语义，不要假设本接口始终返回同一响应结构。

### 8.4 `GET /api/tasks/{task_id}/wells/{well_name}/images`

请求体：无。两种推荐分页方式二选一：

```http
GET /api/tasks/pipeline_C3_C5_20260827_001/wells/C3/images?limit=100&offset=0 HTTP/1.1
Host: 127.0.0.1:8000
```

```http
GET /api/tasks/pipeline_C3_C5_20260827_001/wells/C3/images?page=1&page_size=100 HTTP/1.1
Host: 127.0.0.1:8000
```

| 查询参数 | 默认 | 约束 |
| --- | ---: | --- |
| `limit` | 100 | 1–1000 |
| `offset` | 0 | >= 0 |
| `page` | null | >= 1 |
| `page_size` | null | 1–1000 |

若传 `page_size`，它覆盖 `limit`；若传 `page`，偏移量按 `(page-1) * effective_limit` 计算并覆盖 `offset`。不要混用两套参数。列表只包含 `.bmp/.png/.jpg/.jpeg/.tif/.tiff/.webp`，并排除仍在写入的 `.part` 文件。

响应示例：

```json
{
  "task_id": "pipeline_C3_C5_20260827_001",
  "well_name": "C3",
  "image_dir": "/opt/colony_system/data/interface_tasks/pipeline_C3_C5_20260827_001/C3/images",
  "capture_result_json": "/opt/colony_system/data/interface_tasks/pipeline_C3_C5_20260827_001/C3/scan_result.json",
  "detect_result_json": "/opt/colony_system/data/interface_tasks/pipeline_C3_C5_20260827_001/C3/detect_result.json",
  "compensate_result_json": null,
  "images": [
    "C3_001_row00_col00.bmp",
    "C3_002_row00_col01.bmp"
  ],
  "total": 26,
  "limit": 100,
  "offset": 0,
  "has_more": false
}
```

### 8.5 `GET /api/tasks/{task_id}/wells/{well_name}/images/{filename}`

请求体：无。`filename` 应直接取自上一接口的 `images` 数组并做 URL 编码：

```http
GET /api/tasks/pipeline_C3_C5_20260827_001/wells/C3/images/C3_001_row00_col00.bmp HTTP/1.1
Host: 127.0.0.1:8000
```

响应是文件流，并带下载文件名。源码会拒绝路径穿越和 `.part` 文件；当前下载实现没有再次限制图片后缀，所以服务端集成仍应只使用列表接口返回的图片名。

## 9. 推荐的后端调用顺序

1. 调用 `/health`，确认 API 进程可达。
2. 调用 `/api/hardware/status`，确认没有冲突的普通硬件操作；按业务需要确认录像状态。
3. 生成只含安全字符且全局唯一的 `task_id`。
4. 提交 `/api/tasks/execute`，保存原始请求和 202 响应。
5. 每 0.5–2 秒轮询 `/status`，必须等到终态。
6. 成功后读取 `/result`，有采集产物时再调用孔位图片列表和下载接口。
7. 失败时记录 `task_id`、`error_code`、`error`，并收集 `logs/task.log` 中对应 `task_id=` 的记录及 `logs/api_server.log`；请求排查另查 `logs/api_access.log`，不要仅重试同一个运动请求。

## 10. 源码核对矩阵

| 契约内容 | 源码依据 |
| --- | --- |
| 14 个自定义路由、方法、成功状态 | `workflow/api_server.py` 的 `@app.get/@app.post` |
| 五个声明式请求模型及数值范围 | `workflow/api_models.py` |
| 外层和任务内路径白名单 | `workflow/path_guard.py` |
| 202 入队、队列、取消和状态语义 | `workflow/task_runtime.py`、`workflow/task_store.py` |
| capture/pipeline/compensate/handoff 字段消费 | `workflow/run_task.py` 及各 executor |
| 录像异步契约、默认值、唯一 `.part.avi` 和硬件占用 | `workflow/camera_record_service.py`、`workflow/camera_executor.py`、`workflow/hardware_guard.py` |
| MVS 子进程、状态机、超时隔离、PID 重建和状态字段 | `workflow/camera_process_supervisor.py` |
| 固定往复路径、停止和状态 | `workflow/stage_reciprocation.py` |
| 结果、分页、图片过滤和下载 | `workflow/task_artifacts.py` |
| 错误响应结构 | `workflow/api_errors.py` |

本次核对特别确认了以下容易造成对接误差的行为：

- pipeline 省略 `stages` 时只采集，不会自动检测。
- 普通任务大部分业务错误是 202 后异步失败。
- 往复启动空请求会按默认 800000 参数无限循环。
- 录像启停、往复启停和普通任务外层都拒绝未知字段；`task` 内部仍按后台业务规则校验。
- 内置 4x v2 检测结果不能默认按 `purpose="pick"` 补偿；转 10x 必须用 `purpose="10x_centering"`。
- 单孔与多孔产物目录规则不同；多孔会覆盖单孔级输出路径并按孔派生。
- `page/page_size` 与 `limit/offset` 有明确覆盖优先级。
- 下载端点当前未再次校验图片后缀，客户端应只下载列表返回的文件名。
- 当前无认证/TLS/CORS，硬件状态只是进程内占用状态。

## 11. FastAPI 自动路由

应用使用 FastAPI 默认文档配置，因此还会有以下只读框架路由；它们不是项目业务接口，均无请求体：

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| `GET` | `/openapi.json` | OpenAPI 文档 |
| `GET` | `/docs` | Swagger UI |
| `GET` | `/docs/oauth2-redirect` | Swagger OAuth2 回调页 |
| `GET` | `/redoc` | ReDoc 页面 |

当前 OpenAPI 对 `task` 只能展示为任意对象，且各路由没有声明具体 response model；因此自动文档可用于查看外层 Pydantic 约束，普通任务的业务结构仍以本文和源码为准。
