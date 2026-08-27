# HTTP 功能接口审查与真实硬件联调测试方案

> 服务入口：`workflow.api_server:app`  
> API 版本：`0.3.0`  
> 默认端口：`8000`  
> 审查日期：2026-08-26

## 1. 审查结论

当前服务共有 14 个业务路由，覆盖健康检查、软件占用状态、相机录像、24 孔板固定路径往复、异步任务、取消、结果查询和图片产物访问。FastAPI 还自动提供 `/openapi.json`、`/docs` 和 `/redoc`。

接口契约和无硬件负例已经可以测试；真实硬件必须按“相机 → 单轴 XY → 固定路径 XY → 电机 3/4 → 单孔采集 → 检测/补偿 → handoff”逐级放行，不能直接在 Swagger 中使用默认请求启动运动。

审查时对本机已经运行的 `http://127.0.0.1:8000` 做了只读和请求校验测试，没有连接或驱动硬件：

| 检查项 | 实际结果 |
| --- | --- |
| `GET /health` | HTTP 200，`{"status":"ok"}` |
| OpenAPI | `Colony Workflow API 0.3.0`，14 个业务路径 |
| 往复请求字段 | 仅保留串口、从站、速度、容差、超时和周期数字段 |
| 旧限位字段 `x_min` | HTTP 422，`REQUEST_VALIDATION_FAILED` |
| 任务缺少 `task_id` | HTTP 400，`TASK_ID_REQUIRED` |
| 非法分页 `limit=0` | HTTP 422，`REQUEST_VALIDATION_FAILED` |
| 负例测试后的占用状态 | `busy=false`，没有取得硬件锁 |

### 1.1 当前没有的独立 HTTP 接口

当前没有以下路由：

- 电机 1–4 的当前位置、状态字或报警只读接口；
- 单轴点动、回零、任意绝对位置移动接口；
- 独立物镜切换接口；
- 独立自动调焦接口；
- 硬件急停接口。

`/api/hardware/status` 只表示当前进程内的软件占用锁，不能证明相机在线、串口正常、电机零点正确或电机已经停止。电机 3/4 与自动调焦只能通过观察任务间接触发，首次 HTTP 观察任务前必须先用厂家工具或底层调试方法完成只读位置和低速单轴验证。

### 1.2 网络和进程边界

`--host 0.0.0.0` 会监听所有网卡。本机调用使用 `http://127.0.0.1:8000`，其他机器使用 `http://<服务器局域网 IP>:8000`。

当前服务没有认证、授权或 TLS。所有能访问端口 8000 的客户端都可能提交硬件运动请求，因此只能在隔离联调网段开放，并用 Windows 防火墙限制来源地址。不要将该端口映射到互联网。

服务只支持单进程、单 worker。普通任务只有一个执行线程，最多容纳 16 个等待项。硬件联调时禁止 `--reload`，因为进程重启会中断相机、串口或电机任务。

## 2. 启动与联调基线

PowerShell 中的模块名不能带 Markdown 转义反斜杠，应使用 `workflow.api_server:app`。

在 `ca` 环境下先校验配置，再启动单 worker 服务：

```powershell
Set-Location C:\colony_system

C:\miniforge3\envs\ca\python.exe -m workflow.config_validator `
  --plates config/plates.yaml `
  --handoff config/handoff.yaml `
  --camera config/camera.yaml `
  --autofocus config/autofocus.yaml `
  --objectives config/objectives.yaml

C:\miniforge3\envs\ca\python.exe -m uvicorn workflow.api_server:app `
  --host 0.0.0.0 `
  --port 8000 `
  --workers 1
```

联调终端初始化：

```powershell
$BaseUri = "http://127.0.0.1:8000"
$TerminalStatuses = @("success", "failed", "canceled", "interrupted")
```

| 项目 | 地址或路径 |
| --- | --- |
| Swagger UI | `http://127.0.0.1:8000/docs` |
| OpenAPI | `http://127.0.0.1:8000/openapi.json` |
| ReDoc | `http://127.0.0.1:8000/redoc` |
| API 日志 | `C:\colony_system\logs\api_server.log` |
| 任务索引 | `C:\colony_system\data\task_index` |
| 本文测试产物 | `C:\colony_system\data\interface_tests` |

当前硬件配置基线：

| 硬件 | 配置 |
| --- | --- |
| 相机 | IP `192.168.0.66`，序列号 `DA8583237`，Mono8，5120×5120 |
| 电机 1 / X | slave 1，65536 pulse/mm，物理限位 `[-1295041, 6525977]` |
| 电机 2 / Y | slave 2，131072 pulse/mm，物理限位 `[-1095614, 9241733]` |
| XY 安全范围 | X `[-1163969, 6394905]`，Y `[-964542, 9110661]` |
| 电机 3 / 调焦 | slave 3；切换检查阈值 `-3168285`，小于该值拒绝切换 |
| 电机 4 / 物镜 | slave 4；4X=`166347`，10X=`332695` |
| 交接点 | `(5000000, 0)` |

## 3. 全部功能接口

| 方法 | 路径 | 成功响应 | 是否触发硬件 | 说明 |
| --- | --- | --- | --- | --- |
| GET | `/health` | 200 | 否 | 仅证明 HTTP 进程可响应 |
| GET | `/api/hardware/status` | 200 | 否 | 查询进程内任务、录像、往复占用者 |
| GET | `/api/camera/record/status` | 200 | 否 | 查询当前进程内录像对象状态 |
| POST | `/api/camera/record/start` | 200 | 是，相机 | 打开相机并启动后台录像 |
| POST | `/api/camera/record/stop` | 200 | 是，相机 | 停录像、关相机并提交正式 AVI |
| POST | `/api/stage/reciprocation/start` | 202 | 是，电机 1/2 | 启动 24 孔板六点固定路径运动 |
| POST | `/api/stage/reciprocation/stop` | 200 | 是，电机 1/2 | 协作式请求停止往复 |
| GET | `/api/stage/reciprocation/status` | 200 | 否 | 查询往复状态、目标、周期和缓存位置 |
| POST | `/api/tasks/execute` | 202 | 异步触发 | 提交 capture/pipeline/compensate/handoff |
| POST | `/api/tasks/{task_id}/cancel` | 200 | 不直接急停 | 设置协作取消标志 |
| GET | `/api/tasks/{task_id}/status` | 200 | 否 | 查询任务状态、进度、阶段和错误 |
| GET | `/api/tasks/{task_id}/result` | 200 | 否 | 查询运行摘要或终态结果 |
| GET | `/api/tasks/{task_id}/wells/{well_name}/images` | 200 | 否 | 分页列出孔位图片和结果文件 |
| GET | `/api/tasks/{task_id}/wells/{well_name}/images/{filename}` | 200 | 否 | 下载孔位产物目录中的指定文件 |

### 3.1 通用响应与异步语义

公开错误统一为：

```json
{
  "detail": {
    "error_code": "ERROR_CODE",
    "message": "公开错误信息"
  }
}
```

| HTTP | 典型含义 |
| ---: | --- |
| 400 | 路径越界、缺少任务 ID、配置无法加载 |
| 404 | 任务、孔位、图片或结果不存在 |
| 409 | 硬件占用、录像或往复状态冲突 |
| 422 | 字段类型、范围或未知往复字段错误 |
| 429 | 等待任务队列已满 |
| 503 | 任务运行器未启动或结果文件暂时不可读 |
| 500 | 非法结果文件或未处理异常 |

`POST /api/tasks/execute` 的 202 只表示任务被接受并写入队列。业务字段、模型文件、板型、物镜和部分硬件冲突在后台检查，因此错误请求可能先得到 202，随后状态变为 `failed`。每次提交后都必须轮询到终态，并检查 `error_code`、`error` 和日志。

任务状态包括 `queued`、`running`、`success`、`failed`、`canceled`、`interrupted`。服务重启时遗留的 `queued/running` 会恢复为 `interrupted`。

## 4. 请求模型与边界

### 4.1 相机录像

`POST /api/camera/record/start`：

| 字段 | 默认值或约束 |
| --- | --- |
| `save_path` | `data/camera_records/recording.avi`，只能位于 `data/` 或 `outputs/` |
| `camera_path` | 默认 `config/camera.yaml`，只能位于 `config/` |
| `device_index` | 0–63，可省略 |
| `serial_number` / `ip` | 可覆盖 YAML；联调建议使用 YAML 标定值 |
| `mvs_python_dir` / `pixel_format` | 可覆盖 YAML |
| `exposure_us` | `0 < value <= 10000000` |
| `gain` | 0–60 |
| `fps` | 默认 10，`0 < value <= 240` |
| `bitrate_kbps` | 默认 1000，1–500000 |
| `timeout_ms` | 可省略，`0 < value <= 600000` |

录像先写同目录的 `.part.avi`，正常停止后再替换为正式 `.avi`。当前相机请求模型的未知字段会被静默忽略，调用方必须严格使用上表字段。

### 4.2 位移台往复

`POST /api/stage/reciprocation/start`：

| 字段 | 默认值或约束 |
| --- | --- |
| `port` / `baudrate` | `COM3` / `115200` |
| `x_slave` / `y_slave` | 1 / 2 |
| `profile_vel/acc/dec` | 默认均为 800000，范围 1–10000000 |
| `arrival_tolerance` | 默认 80 pulse |
| `poll_s` / `settle_s` | 0.05 s / 0.2 s |
| `move_timeout_s` | 默认 120 s |
| `max_cycles` | 默认 `null`，一直运行直到 stop |

请求体省略、传 `{}` 或传 `null` 都会采用默认 800000 运动参数并无限循环。真实硬件必须显式使用审核后的低速参数和 `max_cycles=1`。

旧的 `point_a_*`、`point_b_*`、`limit_check_enabled`、`x_min/x_max`、`y_min/y_max`、`safety_margin` 均被禁止，出现任一字段都返回 422，且不会取得硬件锁或启动线程。

固定路径：

| 顺序 | 孔位 | X | Y |
| ---: | --- | ---: | ---: |
| 1 | B2 | 4979233 | 6088062 |
| 2 | B3 | 3852014 | 6088062 |
| 3 | B4 | 2724794 | 6088062 |
| 4 | C2 | 4979233 | 3833623 |
| 5 | C3 | 3852014 | 3833623 |
| 6 | C4 | 2724794 | 3833623 |

### 4.3 普通任务

外层请求：

```json
{
  "task": {},
  "camera_path": null,
  "objectives_path": null,
  "plates_path": null,
  "dump_json": null,
  "persist_result": true
}
```

配置覆盖路径只能位于项目 `config/`；受 HTTP 路径守卫管理的任务产物/输入路径只能位于项目 `data/` 或 `outputs/`。相对路径按 `C:\colony_system` 解析。`detect.model_dir` 和相机 `mvs_python_dir` 是另外校验的本机资源目录，不受该输出目录白名单约束，详见 [HTTP API](http-api.md#32-路径边界)。

`task` 在 OpenAPI 中只是任意对象，服务不会在入队前完整校验业务结构。外层 `ExecuteTaskRequest` 的未知字段目前也会被静默忽略，不能把 Swagger 的请求通过当作业务参数正确。

| `task_type` | 必要功能字段 | 硬件行为 |
| --- | --- | --- |
| `capture` | plate、objective、scope/target、capture、motion、scan | 可能切物镜、调焦、自动调焦、移动 XY、拍照 |
| `pipeline` | capture 字段；显式 `stages` 可增加 detect/compensate | capture 后检测，可选补偿移动 |
| `compensate` | 已验证 detect_result、selector、motion | 可能切物镜并移动 XY 到补偿目标 |
| `handoff` | plate、`handoff.action` | 移动 XY 到交接点，不加载相机/物镜上下文 |

| `observe_scope` | 目标字段 | 说明 |
| --- | --- | --- |
| `single_well` | `target.well_name` | 扫描一个完整培养孔，不是单张拍照或小步移动 |
| `well_list` | 非空 `target.well_list` | 依次扫描指定孔位 |
| `full_plate` | 无 | 按板配置展开全部孔，不适合首次联调 |

`pipeline` 未提供 `stages` 时也只执行 `capture`。需要检测必须显式写 `['capture', 'detect']`；需要补偿必须同时包含 `capture`、`detect` 和 `compensate`。

## 5. 真实硬件前安全门禁

满足以下条件后才允许执行本文带运动的命令：

1. 操作员在设备旁，硬件急停有效，运动区域清空；首次使用空载或无价值测试板。
2. 已用厂家工具只读确认电机 1–4 从站、当前位置、状态字和报警；本项目没有自动 Homing。
3. 已分别完成电机 1/X、2/Y 的低速小行程和方向验证，不能把固定六点往复当作首次动作。
4. 已确认电机 3 当前值不小于 `-3168285`；更小表示更靠近培养板，物镜切换会被拒绝。
5. 已单独验证物镜转盘机械切换不会碰撞；HTTP 没有独立物镜点动接口。
6. 相机序列号 `DA8583237`、IP `192.168.0.66` 与实际设备一致。
7. `/api/hardware/status`、录像和往复状态均为空闲；没有其他客户端操作端口 8000。
8. 首轮任务使用 24 孔板、4X、A1 和低速参数；不执行整板和 10X 全孔扫描。

停止接口和任务取消都是协作式停止，不是硬件急停。当前底层所谓 quick stop 实际写的是 CiA402 `Shutdown 0x06`，不能把 HTTP stop/cancel 当作人身或设备安全措施。

## 6. 第 0 阶段：无硬件接口测试

本阶段只允许健康查询、状态查询和必然在请求校验阶段失败的负例。禁止调用有效的录像 start、往复 start 或任务 execute。

### SW-01：健康与 OpenAPI

```powershell
$Health = Invoke-RestMethod -Uri "$BaseUri/health" -Method Get
if ($Health.status -ne "ok") { throw "health failed" }

$OpenApi = Invoke-RestMethod -Uri "$BaseUri/openapi.json" -Method Get
$Paths = @($OpenApi.paths.PSObject.Properties.Name)
if ($Paths.Count -ne 14) { throw "unexpected business path count: $($Paths.Count)" }

$StageFields = @(
  $OpenApi.components.schemas.StageReciprocationStartRequest.properties.PSObject.Properties.Name
)
$ForbiddenFields = @("point_a_x", "point_b_x", "x_min", "x_max", "y_min", "y_max", "safety_margin")
if (@($ForbiddenFields | Where-Object { $_ -in $StageFields }).Count -ne 0) {
  throw "legacy stage fields are still exposed"
}
```

验收：健康状态为 `ok`，业务路径数为 14，OpenAPI 不再暴露旧点位和限位字段。

### SW-02：空闲状态查询

```powershell
$Hardware = Invoke-RestMethod -Uri "$BaseUri/api/hardware/status" -Method Get
$Camera = Invoke-RestMethod -Uri "$BaseUri/api/camera/record/status" -Method Get
$Stage = Invoke-RestMethod -Uri "$BaseUri/api/stage/reciprocation/status" -Method Get

$Hardware
$Camera
$Stage
```

空闲验收：`hardware.busy=false`、`camera.recording=false`、`stage.status=stopped`。这三项只描述服务内状态，不是物理设备在线证明。

### SW-03：统一错误和旧限位拒绝

```powershell
function Assert-HttpError {
  param(
    [Parameter(Mandatory)] [scriptblock] $Request,
    [Parameter(Mandatory)] [int] $ExpectedStatus,
    [Parameter(Mandatory)] [string] $ExpectedCode
  )

  try {
    & $Request | Out-Null
    throw "request unexpectedly succeeded"
  }
  catch {
    if ($null -eq $_.Exception.Response) { throw }
    $Status = [int]$_.Exception.Response.StatusCode
    $Payload = $_.ErrorDetails.Message | ConvertFrom-Json
    if ($Status -ne $ExpectedStatus -or $Payload.detail.error_code -ne $ExpectedCode) {
      throw "expected HTTP $ExpectedStatus/$ExpectedCode, got HTTP $Status/$($Payload.detail.error_code)"
    }
    "PASS HTTP $Status $($Payload.detail.error_code)"
  }
}

Assert-HttpError -ExpectedStatus 422 -ExpectedCode "REQUEST_VALIDATION_FAILED" -Request {
  Invoke-RestMethod `
    -Uri "$BaseUri/api/stage/reciprocation/start" `
    -Method Post `
    -ContentType "application/json" `
    -Body '{"x_min":0}'
}

Assert-HttpError -ExpectedStatus 400 -ExpectedCode "TASK_ID_REQUIRED" -Request {
  Invoke-RestMethod `
    -Uri "$BaseUri/api/tasks/execute" `
    -Method Post `
    -ContentType "application/json" `
    -Body '{"task":{"task_type":"capture"}}'
}

Assert-HttpError -ExpectedStatus 422 -ExpectedCode "REQUEST_VALIDATION_FAILED" -Request {
  Invoke-RestMethod `
    -Uri "$BaseUri/api/tasks/not_existing/wells/A1/images?limit=0" `
    -Method Get
}

$Hardware = Invoke-RestMethod -Uri "$BaseUri/api/hardware/status" -Method Get
if ($Hardware.busy) { throw "negative tests unexpectedly acquired hardware" }
```

### SW-04：路径边界和未知任务

```powershell
$OutsidePathBody = @{
  task = @{
    task_id = "negative_outside_path"
    task_type = "capture"
    capture = @{ save_dir = "C:/Windows/Temp/colony_test" }
  }
} | ConvertTo-Json -Depth 10

Assert-HttpError -ExpectedStatus 400 -ExpectedCode "PATH_OUT_OF_ALLOWED_ROOT" -Request {
  Invoke-RestMethod `
    -Uri "$BaseUri/api/tasks/execute" `
    -Method Post `
    -ContentType "application/json" `
    -Body $OutsidePathBody
}

Assert-HttpError -ExpectedStatus 404 -ExpectedCode "TASK_NOT_FOUND" -Request {
  Invoke-RestMethod -Uri "$BaseUri/api/tasks/not_existing_20260826/status" -Method Get
}
```

验收：路径越界在入队前返回 400，未知任务返回 404，均不得出现硬件 owner 或任务线程运动。

## 7. 第 1 阶段：相机录像接口

本阶段不移动电机。确认 MVS、网卡和磁盘空间后，使用 YAML 中已标定的序列号/IP，不在请求中重复覆盖身份。

### CAM-01：启动、状态、停止和文件提交

```powershell
$RecordId = Get-Date -Format "yyyyMMdd_HHmmss"
$RecordRelativePath = "data/interface_tests/camera_$RecordId.avi"
$RecordAbsolutePath = Join-Path "C:\colony_system" $RecordRelativePath
$RecordPartPath = Join-Path `
  (Split-Path $RecordAbsolutePath) `
  (([IO.Path]::GetFileNameWithoutExtension($RecordAbsolutePath)) + ".part.avi")
$RecordBody = @{
  save_path = $RecordRelativePath
  fps = 5
  bitrate_kbps = 1000
} | ConvertTo-Json

$Started = $false
try {
  $Start = Invoke-RestMethod `
    -Uri "$BaseUri/api/camera/record/start" `
    -Method Post `
    -ContentType "application/json" `
    -Body $RecordBody
  $Started = $true

  Start-Sleep -Seconds 5
  $Status = Invoke-RestMethod -Uri "$BaseUri/api/camera/record/status" -Method Get
  if (-not $Status.recording) { throw "camera is not recording" }
}
finally {
  if ($Started) {
    $Stop = Invoke-RestMethod -Uri "$BaseUri/api/camera/record/stop" -Method Post
  }
}

if (-not (Test-Path -LiteralPath $RecordAbsolutePath)) { throw "final AVI missing" }
if ((Get-Item -LiteralPath $RecordAbsolutePath).Length -le 0) { throw "final AVI is empty" }
if (Test-Path -LiteralPath $RecordPartPath) { throw "temporary AVI remains" }
```

验收：录像中 `recording=true`，停止后 `recording=false`；正式 AVI 存在、大小大于 0、可播放且无残留 `.part.avi`。同时确认日志选中的相机序列号为 `DA8583237`。

### CAM-02：占用和参数负例

- 录像中再次 start：HTTP 409，`CAMERA_RECORD_BUSY`。
- 空闲时 stop：HTTP 409，`CAMERA_RECORD_STOP_FAILED`。
- `fps=0`、`gain>60` 或 `device_index<0`：HTTP 422，且不打开相机。
- `save_path` 位于 `data/`、`outputs/` 之外：HTTP 400，`PATH_OUT_OF_ALLOWED_ROOT`。

## 8. 第 2 阶段：电机 1/2 和固定往复接口

### XY-01：HTTP 前置的单轴验证

先执行 [XY 标定测试方案](xy_stage_calibration_test_plan.md) 中的 dry-run、X 轴 10 mm、Y 轴 10 mm 测试，确认：

- X 正脉冲使培养板 A1→A2、画面特征向左；
- Y 正脉冲使培养板 A1→B1、画面特征向上；
- 量具实测导程分别为 65536 和 131072 pulse/mm；
- 当前位置、绝对零点、物理限位和安全余量未变化。

### XY-02：固定六点一周期

以下命令会立即移动电机 1/2。操作员必须在设备旁，先核对 B2 首点及完整六点路径。

```powershell
$StageBody = @{
  port = "COM3"
  baudrate = 115200
  x_slave = 1
  y_slave = 2
  profile_vel = 100000
  profile_acc = 100000
  profile_dec = 100000
  arrival_tolerance = 80
  poll_s = 0.05
  settle_s = 0.5
  move_timeout_s = 120
  max_cycles = 1
} | ConvertTo-Json

$Start = Invoke-RestMethod `
  -Uri "$BaseUri/api/stage/reciprocation/start" `
  -Method Post `
  -ContentType "application/json" `
  -Body $StageBody

do {
  Start-Sleep -Milliseconds 500
  $Stage = Invoke-RestMethod -Uri "$BaseUri/api/stage/reciprocation/status" -Method Get
  $Stage | Select-Object status, cycle, completed_targets, current_pos, target, error
} while ($Stage.status -in @("starting", "running", "moving", "stopping"))

# 自然完成后仍调用 stop，做幂等清理和状态确认。
$Stop = Invoke-RestMethod `
  -Uri "$BaseUri/api/stage/reciprocation/stop" `
  -Method Post `
  -ContentType "application/json" `
  -Body '{"join_timeout_s":5}'

if ($Stage.status -ne "stopped") { throw "stage run failed: $($Stage.error)" }
```

验收：目标顺序和坐标与 4.2 完全一致；每点实际位置位于配置安全范围且到位误差不超过 80 pulse；完成一个周期后为 `stopped`；无卡死、碰撞、丢步或异常声响。

### XY-03：协作停止

仅在 XY-02 通过后测试。以 `max_cycles=2` 启动，在已确认的安全行程中调用 stop；允许先返回 `stopping`，必须继续轮询到 `stopped` 并实地确认平台已停止。该用例不验证急停能力。

### XY-04：互斥

- 往复运行中再次启动往复：应返回 409。
- 普通任务占用硬件时启动往复：应返回 409，`HARDWARE_BUSY`。
- 往复占用期间提交普通任务可能先返回 202；后台取得硬件锁失败后应变为 `failed`，`error_code=HARDWARE_BUSY`。

录像 owner 与普通硬件 owner 分开：先录像后允许任务复用相机，也允许纯 XY 往复；任务或往复已经占用普通硬件时再启动录像会返回 `HARDWARE_BUSY`。

## 9. 第 3 阶段：电机 3/4、单孔采集和自动调焦

HTTP 没有独立电机 3/4 路由。第一次观察任务可能连续执行：读取电机 3 → 电机 4 切物镜 → 电机 3 到焦点 → XY 扫描 → 首个扫描点自动调焦 → 拍照。

当前 `data/objective_state.json` 的 `current_objective` 若为 `null`，首次任务必定尝试重新定位物镜和焦点。不得把下面的单孔任务当作电机 3/4 的首次裸机动作。

物镜切换安全规则：

- 只有确实需要切换时才读取电机 3；
- 电机 3 当前值 `< -3168285` 时，禁止电机 4 和电机 3 运动；
- 等于 `-3168285` 时允许；
- 位置读取为 `None` 或抛异常时 fail closed，禁止两轴运动；
- 成功结果中应包含 `focus_position_before_switch`；
- 切换顺序保持电机 4 先运动、电机 3 后运动。

### OBJ-01：厂家/底层单轴前置验收

| 项目 | 目标/验收 |
| --- | --- |
| 电机 3 当前值 | 不小于 `-3168285`，且物理上不接近碰撞区 |
| 4X 焦点 | `-3002685` |
| 10X 焦点 | `-2998604` |
| 4X 自动调焦范围 | `[-3082685, -2922685]` |
| 10X 自动调焦范围 | `[-3078604, -2918604]` |
| 电机 4 的 4X | `166347` |
| 电机 4 的 10X | `332695` |

### TASK-01：24 孔板 A1、4X 单孔采集

该任务会扫描完整 A1 孔，并可能切换物镜和自动调焦。

```powershell
$TaskId = "hw_capture_A1_" + (Get-Date -Format "yyyyMMdd_HHmmss")
$CaptureBody = @{
  task = @{
    task_id = $TaskId
    task_type = "capture"
    plate_type = "24-well"
    objective_name = "4x"
    observe_scope = "single_well"
    target = @{ well_name = "A1" }
    capture = @{
      save_dir = "data/interface_tests/$TaskId"
      filename_pattern = "{well}_{index:03d}_row{row:02d}_col{col:02d}.bmp"
    }
    motion = @{
      port = "COM3"
      baudrate = 115200
      x_slave = 1
      y_slave = 2
      profile_vel = 100000
      profile_acc = 100000
      profile_dec = 100000
      timeout_s = 120
    }
    scan = @{
      overlap = 0.1
      use_objective_fov = $true
      settle_s = 0.8
    }
    output = @{ result_json = "data/interface_tests/$TaskId/result.json" }
  }
  persist_result = $true
} | ConvertTo-Json -Depth 20

$Accepted = Invoke-RestMethod `
  -Uri "$BaseUri/api/tasks/execute" `
  -Method Post `
  -ContentType "application/json" `
  -Body $CaptureBody

if ($Accepted.status -ne "accepted") { throw "task was not accepted" }

do {
  Start-Sleep -Seconds 1
  $TaskStatus = Invoke-RestMethod -Uri "$BaseUri/api/tasks/$TaskId/status" -Method Get
  $TaskStatus | Select-Object task_id, status, progress, current_stage, current_well, error_code, error
} while ($TaskStatus.status -notin $TerminalStatuses)

if ($TaskStatus.status -ne "success") {
  throw "task failed: $($TaskStatus.error_code) $($TaskStatus.error)"
}
```

验收：任务从 accepted/queued 进入 running，最终 `success`、`progress=100`；物镜结果和自动调焦决策符合预期；图片均为 5120×5120 Mono8 BMP；XY、调焦和物镜位置均在审核范围内。

### TASK-02：取消语义

仅在 TASK-01 稳定通过后，对另一个已确认安全的单孔采集任务测试：

```powershell
$Cancel = Invoke-RestMethod -Uri "$BaseUri/api/tasks/$TaskId/cancel" -Method Post
```

接口返回 `cancel_requested` 只表示已设置标志。继续轮询，最终必须为 `canceled`，并包含 `canceled_at` 和 `cancel_reason`。运动可能完成当前动作后才退出，不能把该接口用作急停。

## 10. 第 4 阶段：结果和图片

使用 TASK-01 成功的 `$TaskId`：

```powershell
$Result = Invoke-RestMethod -Uri "$BaseUri/api/tasks/$TaskId/result" -Method Get
$Images = Invoke-RestMethod `
  -Uri "$BaseUri/api/tasks/$TaskId/wells/A1/images?page=1&page_size=20" `
  -Method Get

if ($Images.total -le 0) { throw "no images indexed" }

$FileName = $Images.images[0]
$EncodedName = [uri]::EscapeDataString($FileName)
$DownloadPath = "C:\colony_system\data\interface_tests\download_$FileName"
Invoke-WebRequest `
  -Uri "$BaseUri/api/tasks/$TaskId/wells/A1/images/$EncodedName" `
  -OutFile $DownloadPath
```

验收：

- 活动任务的 result 返回进度摘要且 `result=null`；终态任务返回实际结果；
- 图片按文件名字典序分页，`total/limit/offset/has_more` 正确；
- 列表仅展示支持的图片后缀，并过滤 `.part`；
- 下载文件字节数与原文件一致；
- `../`、绝对路径或 `.part` 下载必须被拒绝，不能越出孔位目录。

单孔任务的图片目录可以正常索引，但当前任务记录里的 `capture_result_json` 可能为 `null`，不要把该字段非空作为单孔采集通过条件。

## 11. 第 5 阶段：检测、补偿和 handoff

### PIPE-01：采集加检测

仓库当前只有模型 manifest 示例，没有可直接运行的已验证权重包。必须先提供真实发布目录，再把 TASK-01 改为：

```json
{
  "task_type": "pipeline",
  "stages": ["capture", "detect"],
  "detect": {
    "entrypoint": "vision.vision.instance_pipeline:process_image",
    "model_dir": "<替换为已验证的 4X 模型发布目录>",
    "provider": "cuda",
    "allow_cpu_fallback": false,
    "deduplication": {
      "calibrated": true,
      "registration_tolerance_mm": 0.1,
      "intersection_over_min_threshold": 0.5
    },
    "save_overlay": true,
    "overlay_source": "vision"
  }
}
```

内置检测契约要求 4X、5120×5120、`allow_downscale=false`。`overlap>0` 时必须使用经过跨视野标定的去重参数。上面的模型路径是占位符，替换前禁止提交。

验收：每孔有 `detect_result.json` 和 overlay；模型版本、provider、输入分辨率、克隆计数和去重统计可追溯；禁止使用旧标定历史结果冒充本次产物。

### COMP-01：独立补偿模板

```powershell
$TaskId = "hw_comp_A1_" + (Get-Date -Format "yyyyMMdd_HHmmss")
$CompensateBody = @{
  task = @{
    task_id = $TaskId
    task_type = "compensate"
    plate_type = "24-well"
    # 已审核的 4x 定位结果用于切到 10x 后对中。
    objective_name = "10x"
    observe_scope = "single_well"
    target = @{ well_name = "A1" }
    motion = @{
      port = "COM3"
      baudrate = 115200
      x_slave = 1
      y_slave = 2
      profile_vel = 100000
      profile_acc = 100000
      profile_dec = 100000
      timeout_s = 120
    }
    compensate = @{
      input_detect_json = "data/interface_tests/<已审核任务>/A1/detect_result.json"
      selector = @{
        mode = "image_and_clone"
        purpose = "10x_centering"
        image_index = 1
        clone_id = "C01"
      }
      closed_loop = @{ enabled = $false }
    }
    output = @{ result_json = "data/interface_tests/$TaskId/result.json" }
  }
  persist_result = $true
} | ConvertTo-Json -Depth 20
```

该模板包含占位路径和克隆 ID，审核 detect_result、补偿方向、比例和目标安全范围后才能提交。内置 4x v2 结果的正式目标是 `is_pickable=false`、可按 `eligible_for_10x_centering=true` 做 10x 对中，因此这里必须显式使用 `purpose="10x_centering"`；只有输入结果确有经审核的 `is_pickable=true` 目标时才改用 `purpose="pick"`。必须用量具或视觉标定板核对真实补偿误差，不能只看驱动器回读。

### HANDOFF-01：load_in / unload_out

Handoff 在电机 1/2 全部测试通过后执行，目标为 `(5000000, 0)`：

```powershell
$TaskId = "hw_handoff_load_" + (Get-Date -Format "yyyyMMdd_HHmmss")
$HandoffBody = @{
  task = @{
    task_id = $TaskId
    task_type = "handoff"
    plate_type = "24-well"
    handoff = @{ action = "load_in" }
    motion = @{
      port = "COM3"
      baudrate = 115200
      x_slave = 1
      y_slave = 2
      profile_vel = 100000
      profile_acc = 100000
      profile_dec = 100000
      timeout_s = 120
    }
    output = @{ result_json = "data/interface_tests/$TaskId/result.json" }
  }
  persist_result = $true
} | ConvertTo-Json -Depth 20

$Accepted = Invoke-RestMethod `
  -Uri "$BaseUri/api/tasks/execute" `
  -Method Post `
  -ContentType "application/json" `
  -Body $HandoffBody

do {
  Start-Sleep -Seconds 1
  $TaskStatus = Invoke-RestMethod -Uri "$BaseUri/api/tasks/$TaskId/status" -Method Get
} while ($TaskStatus.status -notin $TerminalStatuses)

if ($TaskStatus.status -ne "success") {
  throw "handoff failed: $($TaskStatus.error_code) $($TaskStatus.error)"
}
```

分别把 action 改为 `load_in` 和 `unload_out`。验收：两者均到 `(5000000,0)`，到位误差不超过配置的 3000 pulse；只有到位校验通过才返回对应 `ready_state`。

## 12. 已知限制和待加固项

| 项目 | 影响 | 当前测试处置 |
| --- | --- | --- |
| 无认证/TLS | 任意可达客户端可发运动请求 | 隔离网段和防火墙；不暴露公网 |
| 无独立电机只读/点动/回零接口 | 无法只靠 HTTP 完成首次电机准入 | 先用厂家工具或底层单轴测试 |
| 往复空请求默认高速无限循环 | Swagger 误操作风险高 | 真实测试显式低速和 `max_cycles=1` |
| stop/cancel 不是急停 | 请求返回时硬件可能尚未停止 | 操作员持硬件急停并持续轮询/目视确认 |
| `task` 无 OpenAPI 业务 schema | 错误字段可能 202 后才失败 | 轮询终态；保存请求；检查日志 |
| 普通任务外层未知字段被忽略 | 拼写错误可能不被发现 | 严格按本文字段；后续建议 `extra=forbid` |
| 路由未声明具体 response model | OpenAPI 不完整，下载还显示为 JSON | 以本文和实际响应为准；后续补齐契约 |
| 下载实现未限制为图片后缀 | 已知文件名时可下载孔位目录内非 `.part` 文件 | 路径穿越已拦截；后续应增加后缀白名单 |
| 仓库无可运行模型权重 | detect 不能完成真实验收 | 提供并校验发布模型后再开放 PIPE-01 |
| 当前物镜状态可能为 `null` | 首个观察任务会切 M4/M3 并触发自动调焦 | 任务前完成 OBJ-01，全程监护 |
| 扫描覆盖存在已知越界 | 48 孔 4X、所有板型 10X 全孔路径不可直接运行 | 只用已通过规划的 24 孔 4X A1 起步 |

扫描覆盖审查详见 [XY 标定测试方案](xy_stage_calibration_test_plan.md)。越界路径应在运动前失败，不得通过扩大物理限位或缩小安全余量强行放行。

## 13. 总体验收矩阵

| 阶段 | 用例 | 必须结果 | 放行下一阶段 |
| --- | --- | --- | --- |
| 软件 | SW-01～SW-04 | 路由、错误码、路径边界正确，零硬件占用 | 是 |
| 相机 | CAM-01～CAM-02 | 设备身份正确、录像可播放、临时文件清理 | 是 |
| XY 单轴 | XY-01 | 方向、导程、零点、限位、量具误差通过 | 是 |
| XY 固定路径 | XY-02～XY-04 | 六点顺序、到位、停止、互斥通过 | 是 |
| 物镜/调焦 | OBJ-01 | M3/M4 位置和碰撞条件通过 | 是 |
| 单孔采集 | TASK-01～TASK-02 | 24 孔 A1 4X 全链路和取消语义通过 | 是 |
| 产物 | 第 10 节 | 结果、分页、下载和非法文件拒绝通过 | 是 |
| 检测/补偿 | PIPE-01、COMP-01 | 真实模型、去重、补偿方向和精度通过 | 是 |
| 交接 | HANDOFF-01 | load/unload 到交接点并满足 ready 条件 | 分支验收完成 |

任一运动用例出现目标不明、位置读取异常、报警、卡死、异常声响、实际方向相反、到位超差或 `stage_limit_precheck` 失败，应立即停止该阶段，保留请求 JSON、任务 ID、状态响应、产物和 `logs/api_server.log`，排查后从该阶段首个用例重新执行。

## 14. 测试记录模板

| 字段 | 记录内容 |
| --- | --- |
| 用例 ID | 例如 `XY-02` |
| 日期/操作员 |  |
| Git commit / 工作树状态 |  |
| API 版本 |  |
| 设备编号与板型 |  |
| 请求 JSON 保存路径 |  |
| task_id / 录像文件 / 往复开始时间 |  |
| 起始位置与目标位置 |  |
| 期望结果 |  |
| 实际 HTTP 状态、error_code |  |
| 实际位置/量具误差/图片数 |  |
| 日志和结果文件路径 |  |
| 通过/失败 |  |
| 失败原因与复测编号 |  |
