# HTTP API 后端联调契约与请求示例

> 核对基线：2026-08-27，当前工作区源码，FastAPI 应用版本 `0.3.0`。
>
> 本文直接以 `workflow/api_server.py`、`workflow/api_models.py` 和实际执行器为准，不从旧 Markdown 示例反推字段。项目自定义路由共 14 个；FastAPI 自动文档路由另列在文末。真实硬件必须按 [硬件与安全](hardware.md) 分阶段验收。

## 1. 联调前必须确认

- 基础地址示例：`http://127.0.0.1:8000`。
- JSON 接口使用 `Content-Type: application/json`。
- 当前源码没有认证、授权、TLS 或 CORS 中间件。只能部署在受控内网；跨域浏览器调用需要由网关配置 CORS，公网暴露前必须补认证和 TLS。
- 服务只能启动一个 worker。任务队列、硬件锁、录像对象和往复线程都是进程内状态。
- `POST /api/tasks/execute` 的 `202` 只表示入队成功，不代表业务字段、模型、配置或硬件检查通过。调用方必须轮询任务到终态。
- `cancel`、录像 `stop` 和往复 `stop` 都不是硬件急停；运动设备旁仍需可用的物理急停。

启动命令：

```powershell
uvicorn workflow.api_server:app --host 0.0.0.0 --port 8000 --workers 1
```

若 `COLONY_API_WORKERS`、`UVICORN_WORKERS` 或 `WEB_CONCURRENCY` 显式大于 1，服务会拒绝启动。服务还会持有 `data/api_server.lock`，阻止第二个 API 进程。

## 2. 全部项目接口清单

| # | 方法 | 路径 | 请求体 | 成功状态 | 功能 |
| ---: | --- | --- | --- | ---: | --- |
| 1 | `GET` | `/health` | 无 | 200 | HTTP 进程存活检查 |
| 2 | `GET` | `/api/hardware/status` | 无 | 200 | 查询进程内硬件占用者 |
| 3 | `GET` | `/api/camera/record/status` | 无 | 200 | 查询后台录像状态 |
| 4 | `POST` | `/api/camera/record/start` | 必须为 JSON 对象，可传 `{}` | 200 | 启动后台录像 |
| 5 | `POST` | `/api/camera/record/stop` | 无 | 200 | 停止录像并提交正式 AVI |
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

相对路径按项目根目录 `D:/colony_system` 解析。HTTP 层会把下列路径规范化成绝对路径：

| 允许目录 | 字段 |
| --- | --- |
| 只能位于 `config/` | 外层 `camera_path`、`objectives_path`、`plates_path`；录像 `camera_path` |
| 只能位于 `data/` 或 `outputs/` | 外层 `dump_json`、录像 `save_path`；`task.capture.save_dir`、`task.scan.output_json`、`task.detect.output_json`、`task.detect.input_scan_result_json`、`task.compensate.input_detect_json`、`task.compensate.output_json`、`task.compensate.closed_loop.save_dir`、`task.output.result_json/scan_json/detect_json/compensate_json` |

`detect.model_dir` 是模型包目录，`mvs_python_dir` 是本机 SDK 目录；二者不走上述输出路径白名单，但会在各自运行流程中校验。不要允许不可信客户端任意填写这些本机路径。

### 3.3 未知字段处理

| 位置 | 当前行为 |
| --- | --- |
| 往复启动请求 | 未知字段直接返回 422（`extra="forbid"`） |
| 录像启动、往复停止、普通任务外层 | 未知字段目前会被 Pydantic 静默忽略 |
| `task` 内部 | OpenAPI 只声明为任意 JSON 对象，很多业务字段在后台线程中才检查 |

因此，后端不能把“Swagger 提交成功”当成任务结构正确，也不要依赖未知字段被忽略这一现状。

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

`owners` 可能同时列出一个普通硬件操作和一个 `camera_record`。当前实现允许已启动的录像相机被兼容的采集任务复用；该状态仍只是本进程的软件锁，不是硬件在线探测。

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
  "recording": false,
  "background": false,
  "settings": {}
}
```
录像时：
```json
{
    "recording": true,
    "background": true,
    "saved_path": "D:\\colony_system\\data\\camera_records\\recording.avi",
    "frame_rate": 10.0,
    "bitrate_kbps": 1000,
    "frame_count": 164,
    "duration_s": 45.87022662162781,
    "error": null,
    "settings": {
        "save_path": "D:\\colony_system\\data\\camera_records\\recording.avi",
        "mvs_python_dir": "D:/colony_system/MvImport",
        "device_index": 0,
        "serial_number": "DA8583237",
        "camera_ip": "192.168.0.66",
        "pixel_format": "mono8",
        "exposure_us": 5000,
        "gain": 0.0,
        "fps": 10.0,
        "bitrate_kbps": 1000,
        "timeout_ms": null
    }
}
```
### 5.2 `POST /api/camera/record/start`

请求体本身必传，但所有字段都有默认值；最小合法请求体是：

```json
{}
```

联调建议显式请求体：

```json
{
  "save_path": "data/camera_records/backend_joint_001.avi",
  "camera_path": "config/camera.yaml",
  "device_index": 0,
  "serial_number": "DA8583237",
  "ip": "192.168.0.66",
  "mvs_python_dir": "D:/colony_system/MvImport",
  "pixel_format": "mono8",
  "exposure_us": 5000,
  "gain": 0.0,
  "fps": 10.0,
  "bitrate_kbps": 1000,
  "timeout_ms": 5000
}
```

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
| `timeout_ms` | integer/null | null | 非 null 时 `0 < value <= 600000` |

录像先写同目录的 `.part.avi`，正常停止后再提交为正式 `.avi`。同一进程只能有一个后台录像。

### 5.3 `POST /api/camera/record/stop`

请求体：无。

```http
POST /api/camera/record/stop HTTP/1.1
Host: 127.0.0.1:8000
Content-Length: 0
```

返回：
```json
{
    "status": "stopped",
    "video": {
        "saved_path": "D:\\colony_system\\data\\camera_records\\recording.avi",
        "width": 5120,
        "height": 5120,
        "pixel_type": 17301505,
        "frame_rate": 10.0,
        "bitrate_kbps": 1000,
        "frame_count": 291,
        "duration_s": 81.45140290260315,
        "timestamp_started": 1787793958.6808152,
        "timestamp_finished": 1787794040.1322181
    },
    "settings": {
        "save_path": "D:\\colony_system\\data\\camera_records\\recording.avi",
        "mvs_python_dir": "D:/colony_system/MvImport",
        "device_index": 0,
        "serial_number": "DA8583237",
        "camera_ip": "192.168.0.66",
        "pixel_format": "mono8",
        "exposure_us": 5000,
        "gain": 0.0,
        "fps": 10.0,
        "bitrate_kbps": 1000,
        "timeout_ms": null
    }
}
```
没有活动录像时不是幂等成功，而是返回 409 `CAMERA_RECORD_STOP_FAILED`。成功时会停止后台线程、关闭相机，并把 `.part.avi` 提交为正式文件。

## 6. 位移台固定往复接口

该功能不是任意两点运动。源码固定读取 `config/plates.yaml` 的 `24-well`，依次走 `B2`、`B3`、`B4`、`C2`、`C3`、`C4`，并使用板型的 `stage_limits` 做预检和运行时保护。请求不能覆盖孔位、坐标或限位。

### 6.1 `POST /api/stage/reciprocation/start`

请求体可以完全省略、传 `null` 或传 `{}`。这三种方式都会采用默认速度并持续循环，真实设备联调不应这样调用。

真实硬件的显式单周期示例：

```json
{
  "port": "COM3",
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
| `port` | string | `COM3` | 长度 1–64 |
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

提交成功响应示例：

```json
{
  "task_id": "capture_A1_20260827_001",
  "status": "accepted",
  "task_type": "capture",
  "observe_scope": "single_well",
  "objective_name": "4x",
  "message": "task accepted",
  "result_json_path": "D:/colony_system/data/interface_tasks/capture_A1_20260827_001/result.json"
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
| `motion.port` | `COM3` | XY Modbus 串口 |
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
      "port": "COM3",
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
      "port": "COM3",
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
      "overlay_source": "vision",
      "detect_well_border": true,
      "well_border_margin_mm": 0.0,
      "well_border_margin_px": 30.0
    },
    "output": {
      "result_json": "data/interface_tasks/pipeline_C3_C5_20260827_001/result.json"
    }
  },
  "persist_result": true
}
```

内置模型检测契约：

- 只允许 `objective_name="4x"`。
- 相机必须固定为 5120×5120，且 `camera.resolution.allow_downscale=false`。
- `detect.model_dir` 必填。
- `scan.overlap > 0` 时，必须设置 `deduplication.calibrated=true`。
- `calibrated=true` 时必须显式传正数 `registration_tolerance_mm`。
- `intersection_over_min_threshold` 必须在 `(0, 1]`。
- `provider` 常用 `cuda`、`cpu`、`auto`；实际支持性由模型运行时检查。生产默认用 `cuda`，`allow_cpu_fallback` 默认 false。

检测字段中，`save_overlay` 默认 true，`overlay_source` 默认 `vision`；可选 `workflow`。工作流绘图时还会读取 `draw_bbox`、`draw_center`、`draw_image_center`。`detect.output_json` 适用于单孔；多孔任务会固定派生为 `<capture.save_dir>/<well>/detect_result.json`。

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
      "port": "COM3",
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

### 7.9 机械臂交接完整示例

`handoff.action` 只支持 `load_in` 和 `unload_out`。点位来自本机 `config/handoff.yaml`，请求不能直接覆盖交接坐标。

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
      "port": "COM3",
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

下料只需生成新任务 ID，并把 `handoff` 改为：

```json
{
  "action": "unload_out"
}
```

`motion` 可省略部分字段并回退到 `handoff.yaml`，但联调请求建议显式保留经过审核的参数。

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
  "image_dir": "D:/colony_system/data/interface_tasks/pipeline_C3_C5_20260827_001/C3/images",
  "capture_result_json": "D:/colony_system/data/interface_tasks/pipeline_C3_C5_20260827_001/C3/scan_result.json",
  "detect_result_json": "D:/colony_system/data/interface_tasks/pipeline_C3_C5_20260827_001/C3/detect_result.json",
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
7. 失败时记录 `task_id`、`error_code`、`error` 和 `logs/api_server.log`，不要仅重试同一个运动请求。

## 10. 源码核对矩阵

| 契约内容 | 源码依据 |
| --- | --- |
| 14 个自定义路由、方法、成功状态 | `workflow/api_server.py` 的 `@app.get/@app.post` |
| 四个声明式请求模型及数值范围 | `workflow/api_models.py` |
| 外层和任务内路径白名单 | `workflow/path_guard.py` |
| 202 入队、队列、取消和状态语义 | `workflow/task_runtime.py`、`workflow/task_store.py` |
| capture/pipeline/compensate/handoff 字段消费 | `workflow/run_task.py` 及各 executor |
| 录像默认值、覆盖顺序和 `.part.avi` | `workflow/camera_record_service.py`、`workflow/camera_executor.py` |
| 固定往复路径、停止和状态 | `workflow/stage_reciprocation.py` |
| 结果、分页、图片过滤和下载 | `workflow/task_artifacts.py` |
| 错误响应结构 | `workflow/api_errors.py` |

本次核对特别确认了以下容易造成对接误差的行为：

- pipeline 省略 `stages` 时只采集，不会自动检测。
- 普通任务大部分业务错误是 202 后异步失败。
- 往复启动空请求会按默认 800000 参数无限循环。
- 往复启动拒绝未知字段，其他几个 Pydantic 请求模型目前会忽略未知字段。
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
