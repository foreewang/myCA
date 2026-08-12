# Colony System 使用说明

本项目是一套培养板克隆/菌落自动化工作流系统，用于在位移台、海康工业相机、物镜切换轴、调焦轴和第三方自动对焦模块协同下，按任务配置完成：

- 培养孔扫描路径规划
- XY 位移台绝对运动
- 海康 MVS 相机拍照和录像
- 单图/批量克隆识别与轮廓定位
- 可选自动对焦
- 基于识别结果的目标补偿定位
- 机械臂上下料对接位置移动
- API 单工作线程任务队列、进度查询和协作式取消
- 位移台固定孔位往复扫描与硬件占用状态查询

系统既支持命令行本地执行，也支持 FastAPI HTTP 服务提交任务。核心入口是 `workflow/run_task.py` 中的 `execute_task_request`。

## 项目结构

```text
colony_system/
├─ workflow/                  # 任务编排与执行层
│  ├─ run_task.py              # CLI 入口与 execute_task_request
│  ├─ api_server.py            # FastAPI 服务，任务、硬件、图片、录像接口
│  ├─ api_models.py            # HTTP 请求体模型与参数范围校验
│  ├─ task_runtime.py           # 单工作线程任务队列、进度和取消生命周期
│  ├─ task_store.py             # 任务账本、状态恢复和结果索引
│  ├─ task_artifacts.py         # 任务结果、孔位图片和文件下载
│  ├─ hardware_guard.py         # 任务、录像和位移台操作的进程内互斥
│  ├─ process_guard.py          # 单 worker 与单 API 进程保护
│  ├─ path_guard.py             # HTTP 请求配置/输出路径边界检查
│  ├─ file_io.py               # JSON/Text 原子写入与重试读取
│  ├─ config_validator.py      # YAML 机器校验，提前拦截配置错误
│  ├─ camera_executor.py       # 相机打开、拍照、共享录像相机管理
│  ├─ autofocus_executor.py    # 第三方自动对焦适配
│  ├─ scan_planner.py          # 孔内扫描点规划与限位预检查
│  ├─ scan_executor.py         # 位移台移动、自动对焦、相机拍照
│  ├─ stage_executor.py        # XY 同步绝对运动、停止、限位和到位检查
│  ├─ stage_reciprocation.py   # 后台位移台往复扫描控制器
│  ├─ detect_api.py            # vision 检测入口动态加载与结果归一化
│  ├─ detect_executor.py       # 批量检测、overlay 输出、detect_result 生成
│  ├─ compensate_executor.py   # 按检测目标计算补偿位移
│  ├─ objective_executor.py    # 物镜轴与调焦轴切换
│  ├─ handoff_executor.py      # 上下料对接位移动
│  └─ plate_geometry.py        # 板型、孔位、脉冲/mm 等几何计算
├─ devices/
│  ├─ camera_controller.py     # 海康 MVS 相机控制、拍照、SDK 录像封装
│  └─ motion/                  # Modbus RTU 与 MotorManager
├─ vision/
│  ├─ run_detect.py            # 单图检测调试入口
│  └─ vision/                  # 图像检测流水线
├─ config/
│  ├─ camera.yaml              # 相机配置
│  ├─ objectives.yaml          # 物镜视野、切换点、状态文件
│  ├─ plates.yaml              # 板型几何参数和安全限位
│  ├─ autofocus.yaml           # 自动对焦策略与第三方模块配置
│  └─ handoff.yaml             # 机械臂上下料对接点
├─ data/                       # 任务索引、运行输出、测试输出
│  └─ objective_state.json     # 当前物镜状态，应与真实硬件状态一致
├─ third_party/XWJJJ260511/    # 第三方自动对焦模块
├─ tools/                      # 测试和辅助脚本
├─ tests/test_core_workflow.py # 核心工作流自动化测试
└─ README.md
```

仓库当前不附带可直接运行的 `task_*.json` 任务模板。CLI 任务文件需要按下文格式自行创建；HTTP 调用则直接在请求体的 `task` 字段中提交相同结构。

## 主要能力

### 任务类型

| task_type | 说明 |
| --- | --- |
| `capture` | 只执行采集，默认阶段为 `capture` |
| `pipeline` | 执行阶段流水线，可包含 `capture`、`detect`、`compensate` |
| `compensate` | 独立补偿任务，读取已有检测结果后移动到目标中心 |
| `handoff` | 移动到机械臂上下料对接点，支持 `load_in` 和 `unload_out` |

### 观察范围

| observe_scope | 说明 |
| --- | --- |
| `single_well` | 单个孔位 |
| `well_list` | 任务中指定的多个孔位 |
| `full_plate` | 根据板型配置展开整板孔位 |

### 检测与补偿

当前仓库中的检测实现入口是：

```text
vision.vision.detect_pipeline:process_image
```

`workflow.detect_api` 同时兼容历史写法 `vision.detect_pipeline:process_image`，未显式配置 `detect.entrypoint` 时也会自动查找内置入口。

workflow 会通过 `workflow.detect_api` 调用 vision 算法，并将结果归一化为 `detect_result.json`。检测 overlay 默认使用 vision 自身输出的 `06_overlay.bmp`，路径会写入每张图片的 `overlay_image_path`。

vision 当前采用 OpenCV 规则算法：

- 粗检测以 strict dark-core 为主路径，并用 texture-density fallback 补充浅色/纹理型克隆
- 过滤前景比例异常、bbox 面积比例异常、触边候选和大面积异常候选
- 在 ROI 内使用径向轮廓搜索、中心重定位，并可选用 GrabCut 做边缘贴合
- 根据可见孔边界距离输出 `near_well_border` / `is_pickable`
- 无克隆/背景图可以直接返回 `component_count=0`
- 中心点优先使用 `safe_point` / `dark_core_center_pixel`
- 保留 `contour_center_pixel` 便于对比最终轮廓质心
- 每个 component 输出 `confidence` 和 `is_valid_for_compensation`

补偿阶段只会把 `is_pickable=true` 的候选加入选择器。当前内置 vision 算法仅在候选有效且不靠近孔边缘时设置该字段，因此孔边缘、触边、低置信度或异常候选不会进入补偿选择。

## 环境准备

代码使用 `X | None` 等类型语法，要求 Python 3.10 或更高版本。建议在项目根目录创建虚拟环境并安装锁定在 `requirements.txt` 中的运行依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

当前运行依赖包括：

```text
fastapi
uvicorn
pydantic
pyyaml
numpy
opencv-python
pillow
pymodbus
pyserial
matplotlib
```

运行测试还需要安装 pytest：

```powershell
python -m pip install pytest
python -m pytest -q
```

硬件和 SDK 依赖：

- 海康 MVS Python SDK，配置见 `config/camera.yaml`
- `camera.mvs_python_dir` 是 MVS Python SDK 导入目录的标准字段；旧字段 `mvs_sdk_path` 仅作为兼容别名
- 相机选择优先级为 `serial_number > ip > device_index`
- 当前生产采集链路要求 `pixel_format: mono8`
- Modbus RTU 默认通信参数为 `COM3`、`115200 bps`、8 数据位、1 停止位、无校验（8N1）
- 电机 1 默认为软件 X 轴，负责培养板列方向 `A1 -> A2` 和图像左右方向
- 电机 2 默认为软件 Y 轴，负责培养板行方向 `A1 -> B1` 和图像上下方向
- 电机 3 为细准焦调焦轴
- 电机 4 为物镜切换轴

X/Y 是软件业务坐标，不按丝杆或位移台机械长短自动判断。任务中的 `motion.x_slave/y_slave`、`config/handoff.yaml` 和位移台往复接口都允许覆盖默认从站号；一旦交换 X/Y，从站、A1 坐标、X/Y 限位、handoff 点位、扫描方向和补偿方向必须一起重新标定。

当前电机驱动假设设备使用项目中硬编码的 Modbus 寄存器映射、32 位高字在前字序和 CiA-402 PP 位置控制序列。更换驱动器时，应先核对 `devices/motion/modbus.py`；协议、寄存器或单位不同不能仅靠修改 YAML 适配。

## 配置文件与机器校验

配置文件位于 `config/`。主流程会在运行前对高风险配置做机器校验，建议现场改完 YAML 后先手动执行一次：

```powershell
python -m workflow.config_validator
```

也可以只校验部分配置：

```powershell
python -m workflow.config_validator --camera config/camera.yaml --objectives config/objectives.yaml
python -m workflow.config_validator --plates config/plates.yaml
python -m workflow.config_validator --autofocus config/autofocus.yaml --objectives config/objectives.yaml --camera config/camera.yaml
python -m workflow.config_validator --handoff config/handoff.yaml
```

校验重点：

- `camera.yaml`：MVS SDK 路径、相机序列号/IP/index、分辨率、曝光、增益、`objective_settings` 覆盖所有物镜、`trigger_mode`、`pixel_format`、保存格式
- `plates.yaml`：固定板型、孔板几何、轴方向、`stage_limits`、`runtime_guard`、旧字段和错误缩进
- `autofocus.yaml`：触发策略、MVS 相机配置、曝光关闭自动模式、物镜覆盖、调焦范围、硬件串口一致性
- `handoff.yaml`：硬件从站、点位、动作引用、`settle_s`、`arrival_tolerance_pulse`

## 命令行使用

CLI 支持 JSON 或 YAML 任务文件。先自行创建一个任务文件，例如 `data/task_capture_single_well.json`：

```json
{
  "task": {
    "task_id": "capture_A1_local_001",
    "task_type": "capture",
    "plate_type": "24-well",
    "objective_name": "4x",
    "observe_scope": "single_well",
    "target": {
      "well_name": "A1"
    },
    "capture": {
      "save_dir": "C:/colony_system/data/local_tasks/capture_A1_local_001/images",
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
      "settle_s": 0.8,
      "output_json": "C:/colony_system/data/local_tasks/capture_A1_local_001/scan_result.json"
    },
    "output": {
      "result_json": "C:/colony_system/data/local_tasks/capture_A1_local_001/result.json"
    }
  }
}
```

示例中的运动速度、坐标、限位和从站号来自当前旧设备配置。接入新电机前必须完成低速单轴测试和重新标定，不能直接把这些数值用于首次运动。

在项目根目录执行：

```powershell
cd C:\colony_system
python workflow/run_task.py --task data/task_capture_single_well.json
```

可覆盖配置路径：

```powershell
python workflow/run_task.py `
  --task data/task_capture_single_well.json `
  --camera config/camera.yaml `
  --objectives config/objectives.yaml `
  --plates config/plates.yaml `
  --dump-json data/my_result.json
```

`handoff` 任务文件只需要提供 handoff 任务字段；例如先创建 `data/task_handoff_load_in.json`：

```json
{
  "task": {
    "task_id": "handoff_load_in_local_001",
    "task_type": "handoff",
    "plate_type": "24-well",
    "handoff": {
      "action": "load_in"
    },
    "output": {
      "result_json": "C:/colony_system/data/local_tasks/handoff_load_in_local_001/result.json"
    }
  }
}
```

```powershell
python workflow/run_task.py --task data/task_handoff_load_in.json --handoff config/handoff.yaml
```

单图 vision 检测调试：

```powershell
python vision/run_detect.py path\to\image.bmp --out_dir data/vision_debug
```

dark-core、ROI 轮廓和边缘细化相关参数也可以在单图调试时覆盖：

```powershell
python vision/run_detect.py path\to\image.bmp `
  --out_dir data/vision_debug `
  --seed_quantile 0.12 `
  --core_density_min 80 `
  --min_foreground_ratio 0.025 `
  --max_bbox_area_ratio 0.30 `
  --radial_mode hybrid `
  --edge_refine_method hybrid `
  --edge_refine_iterations 2
```

仓库当前没有独立的相机拍照/录像测试脚本。拍照通过 `capture` 任务测试；后台录像通过下文的 HTTP 录像接口测试。

## HTTP 服务

启动服务：

```powershell
uvicorn workflow.api_server:app --host 0.0.0.0 --port 8000 --workers 1
```

生产部署必须保持 `--workers 1`。当前系统只控制一套相机、位移台、物镜和调焦硬件，`workflow/hardware_guard.py` 的硬件互斥锁是进程内锁，多 worker 会让每个 worker 各自持有一套内存锁，无法互相感知。API 服务启动时会检查 `COLONY_API_WORKERS`、`UVICORN_WORKERS`、`WEB_CONCURRENCY`；如果显式配置大于 1，会拒绝启动。同时服务会持有 `data/api_server.lock`，用于阻止多个 API worker/process 同时启动。

开发时如果只改 Python 代码，可以使用自动重载：

```powershell
uvicorn workflow.api_server:app --host 0.0.0.0 --port 8000 --reload
```

设备联调时不建议使用 `--reload`，因为 reload 会重启进程，可能中断相机、串口、电机任务。生产环境不要使用 `--reload`，也不要使用 `--workers 2` 或更高。

API 普通任务由一个后台工作线程串行执行，队列默认最多容纳 16 个待执行任务。`POST /api/tasks/execute` 返回 202 只表示任务已进入队列，不表示硬件动作已经完成。任务记录状态包括：

| status | 说明 |
| --- | --- |
| `queued` | 已受理，等待后台工作线程执行 |
| `running` | 正在执行 |
| `success` | 执行成功 |
| `failed` | 执行失败 |
| `canceled` | 已在安全检查点响应取消 |
| `interrupted` | 服务异常退出或重启时发现原任务未正常结束 |

取消是协作式取消：`POST /api/tasks/{task_id}/cancel` 会设置取消标记，任务在下一处安全检查点停止，不保证请求返回瞬间硬件已经停止。服务启动时会把遗留的 `queued/running` 记录恢复为 `interrupted`。

HTTP 请求中的配置路径只能位于项目 `config/` 下；任务输入/输出路径只能位于项目 `data/` 或 `outputs/` 下。相对路径按项目根目录解析。CLI 入口不经过这层 HTTP 路径边界检查，但仍应使用项目目录内的配置和产物路径。

### 日志与文件 IO 观测

API 日志默认写入：

```text
C:/colony_system/logs/api_server.log
```

内网调试默认保留详细日志，方便定位任务 ID、配置路径和产物路径。生产部署时建议开启日志脱敏：

```powershell
$env:COLONY_LOG_REDACT_SENSITIVE="1"
```

开启后，API 错误日志中的绝对路径和 `task_id` 会被脱敏，例如：

```text
task_id=<task:8f3a21c9> path=<DATA_ROOT>/<redacted>
```

可选开关：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `COLONY_LOG_REDACT_SENSITIVE` | `0` | 是否启用生产脱敏 |
| `COLONY_LOG_REDACT_PATHS` | `1` | 脱敏路径 |
| `COLONY_LOG_REDACT_TASK_ID` | `1` | 脱敏任务 ID |
| `COLONY_FILE_IO_SLOW_WARNING_MS` | `500` | JSON/Text 文件读写重试超过该毫秒数时记录 warning |

任务索引和结果 JSON 使用原子写入与短暂重试读取。若 Windows 文件锁、杀毒软件或高频轮询导致读写短暂阻塞，日志会出现：

```text
FILE_IO_SLOW: op=read_json path_kind=task_record path=<DATA_ROOT>/<redacted> elapsed_ms=1050.3 attempts=22 last_error=PermissionError
```

这类日志表示文件最终可能已经读写成功，但耗时超过阈值。不要先盲目缩短重试时间；应先根据该日志判断是否需要降低前端轮询频率、排查杀毒扫描，或后续改用 SQLite/数据库队列。

### 任务接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/health` | 健康检查 |
| `GET` | `/api/hardware/status` | 查询任务、录像或位移台操作的硬件占用状态 |
| `POST` | `/api/tasks/execute` | 异步提交任务，返回 accepted |
| `POST` | `/api/tasks/{task_id}/cancel` | 请求在下一处安全检查点取消任务 |
| `GET` | `/api/tasks/{task_id}/status` | 查询任务状态、进度和当前阶段 |
| `GET` | `/api/tasks/{task_id}/result` | 查询任务结果，运行中时返回进度摘要 |
| `GET` | `/api/tasks/{task_id}/wells/{well_name}/images` | 分页列出孔位图片和结果文件，支持 `limit/offset` 或 `page/page_size` |
| `GET` | `/api/tasks/{task_id}/wells/{well_name}/images/{filename}` | 下载孔位图片 |
| `POST` | `/api/stage/reciprocation/start` | 启动后台位移台往复扫描，返回 202 |
| `POST` | `/api/stage/reciprocation/stop` | 请求停止位移台往复扫描 |
| `GET` | `/api/stage/reciprocation/status` | 查询往复扫描状态、当前位置和进度 |

任务请求体示例：

```json
{
  "task": {
    "task_id": "pipeline_C5_detect_http_001",
    "task_type": "pipeline",
    "stages": ["capture", "detect"],
    "plate_type": "24-well",
    "objective_name": "4x",
    "observe_scope": "well_list",
    "target": {
      "well_list": ["C5"]
    },
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
      "entrypoint": "vision.vision.detect_pipeline:process_image",
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

对于 `well_list` 和 `full_plate`，每个孔位的 `scan_result.json`、`detect_result.json` 和 `compensate_result.json` 会由 workflow 自动改写到 `<capture.save_dir>/<well_name>/`，因此不要依赖任务中单个 `detect.output_json` 作为多孔任务的最终路径。顶层 `output.result_json` 仍用于保存总结果。

### 录像接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/camera/record/start` | 打开相机并启动后台录像 |
| `GET` | `/api/camera/record/status` | 查询当前录像状态 |
| `POST` | `/api/camera/record/stop` | 停止后台录像并关闭共享相机 |

PowerShell 启动录像示例：

```powershell
$body = @{
  save_path = "C:/colony_system/data/camera_records/test_record.avi"
  camera_path = "C:/colony_system/config/camera.yaml"
  ip = "192.168.0.253"
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

录像期间可以继续提交 `capture` / `pipeline` 任务。相机拍照会复用正在录像的相机对象，通过录像线程提供的帧保存快照，避免重复打开海康相机造成冲突。

录像接口读取 `camera.yaml` 前会执行机器校验。请求体里的 `mvs_python_dir`、`serial_number`、`ip`、`device_index`、`pixel_format` 会覆盖配置文件中的对应字段，并再次校验后才打开相机。

后台录像会先写入同目录的 `.part.avi` 临时文件，停止录像成功后再替换为正式 `.avi`。图片列表和下载接口会过滤或拒绝 `.part` 文件，避免前端读取到未完成产物。

停止录像：

```powershell
Invoke-RestMethod `
  -Uri "http://127.0.0.1:8000/api/camera/record/stop" `
  -Method Post
```

### 位移台往复接口

当前往复控制器不是任意两点往复：它固定读取 `config/plates.yaml` 中的 `24-well` 配置，并依次移动到 `B2`、`B3`、`B4`、`C2`、`C3`、`C4`，完成后从头循环。省略 `max_cycles` 时会持续运行，直到调用 stop。

`StageReciprocationStartRequest` 目前仍保留 `point_a_x/point_a_y/point_b_x/point_b_y` 兼容字段，但控制器不会使用这些字段生成目标点。需要自定义路径时应先扩展 `workflow/stage_reciprocation.py`，不要把该接口当作通用两点运动接口。

示例仅适用于已经完成坐标、限位和运动参数标定的设备：

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

新电机首次联调不要使用该接口；应先完成只读通信、单轴低速小位移、方向、脉冲/mm、绝对零点和软硬限位验证。

### 位移台两点往复精度测试

`tools/stage_reciprocation_accuracy.py` 用于测试任意已标定 A/B 点之间的往复精度。一个周期为 `A -> B -> A`，脚本分别统计两个端点的平均误差、标准差、极差、最大绝对误差和 RMSE，并输出：

- `moves.csv`：每次运动的目标、运动前后位置、到位误差和耗时
- `summary.json`：测试参数以及 pulse/mm 两种单位的统计结果

脚本默认是 dry-run，不连接硬件。下面的坐标仅用于展示命令格式，必须替换为当前设备已经标定且位于软件安全限位内的点位：

```powershell
python tools/stage_reciprocation_accuracy.py `
  --axis x `
  --plate-type 24-well `
  --point-a-x 1000000 --point-a-y 1000000 `
  --point-b-x 1200000 --point-b-y 1000000 `
  --cycles 20 `
  --warmup-cycles 2
```

确认 dry-run 输出中的端点、行程、从站号和安全范围后，在同一命令末尾追加 `--execute`。测试X轴时两个点的Y必须相同；测试Y轴时两个点的X必须相同；`--axis xy` 允许两个坐标同时变化。默认保持当前标准映射：软件X/电机1使用 `slave=1`，软件Y/电机2使用 `slave=2`。

该脚本统计的是驱动器返回的位置误差。验证丝杆间隙和真实物理重复定位精度时，还需要同步记录量表、光栅尺或视觉标定板读数。

## 自动对焦

- `workflow/run_task.py` 会读取 `config/autofocus.yaml` 生成 `autofocus_decision`。
- 真正自动对焦发生在 `scan_executor.py`：第一个扫描点完成 XY 移动并稳定后、第一张拍照前执行。
- 默认触发策略是物镜发生切换后自动对焦；`always_before_capture` 只会让自动对焦决策成立，实际执行频率仍由 `trigger.scope` 控制，默认每孔第一个扫描点执行一次。
- `config/autofocus.yaml` 会在任务读取和执行前做机器校验，包括 MVS SDK 路径、相机曝光、物镜覆盖、调焦范围和硬件串口一致性。
- 录像期间触发 autofocus 时，会复用当前正在录像的相机对象，避免第三方 autofocus 模块独占打开相机造成冲突。
- autofocus 复用录像相机时，临时采样图保存到 `data/autofocus_recording_tmp`。

## 任务配置要点

任务文件顶层必须包含 `task` 字段。常见字段如下：

| 字段 | 说明 |
| --- | --- |
| `task_id` | 任务唯一标识，HTTP 任务索引会使用它作为文件名 |
| `task_type` | `capture`、`pipeline`、`compensate` 或 `handoff` |
| `plate_type` | 板型名称，如 `24-well` |
| `objective_name` | 物镜名称，如 `4x`、`10x` |
| `objective` | 兼容旧任务文件的别名，已废弃；新任务请使用 `objective_name` |
| `observe_scope` | `single_well`、`well_list`、`full_plate` |
| `target.well_name` | 单孔任务目标孔位 |
| `target.well_list` | 多孔任务孔位列表 |
| `stages` | 流水线阶段，如 `["capture", "detect"]` |
| `capture.save_dir` | 单孔图片目录；多孔任务的基础保存目录 |
| `capture.filename_pattern` | 图片命名模板 |
| `motion.port/baudrate` | XY 位移台 Modbus 串口和波特率 |
| `motion.x_slave/y_slave` | 软件 X/Y 轴从站号，默认分别为 1/2 |
| `motion.profile_vel/profile_acc/profile_dec` | PP 位置模式速度、加速度和减速度；采集/补偿时必须提供 |
| `motion.timeout_s` | 单次 XY 移动超时，默认 120 秒 |
| `scan.overlap` | 扫描重叠率，要求 `0 <= overlap < 1` |
| `scan.use_objective_fov` | 是否使用当前物镜视野生成扫描步长 |
| `scan.output_json` | 单孔采集结果 `scan_result.json` 输出路径 |
| `detect.entrypoint` | 检测入口，格式为 `模块路径:函数名` |
| `detect.output_json` | 单孔检测结果 JSON 输出路径；多孔任务会改写为孔位子目录 |
| `detect.save_overlay` | 是否保存检测标注图，默认开启 |
| `detect.overlay_source` | `vision` 或 `workflow`，默认 `vision` |
| `detect.detect_well_border` | 是否启用可见孔边界检测，默认开启 |
| `detect.well_border_margin_mm` | 按物理距离判定靠近孔边缘的安全边距 |
| `detect.well_border_margin_px` | 未使用物理边距时的像素安全边距 |
| `compensate.selector` | 补偿目标选择策略 |
| `compensate.scale` | 补偿倍率修正，例如 `{ "x": 0.79, "y": 1.0 }` |
| `compensate.closed_loop` | 闭环补偿配置 |
| `compensate.input_detect_json` | 独立补偿任务读取的已有检测结果 |
| `output.result_json` | 总结果 JSON 输出路径 |
| `handoff.action` | `load_in` 或 `unload_out` |

相机参数来自 `camera.yaml`，任务运行时会按当前物镜选择 `camera.objective_settings.<objective_name>` 中的曝光和增益。底层相机控制器会强制设置并校验 Mono8，录像逐帧也会校验帧格式和长度。

### 补偿选择器

`compensate.selector.mode` 支持：

| mode | 说明 |
| --- | --- |
| `first` | 使用第一个有效克隆 |
| `largest_area` | 使用面积最大的有效克隆 |
| `nearest_image_center` | 使用离图像中心最近的有效克隆 |
| `clone_id` | 按 `clone_id` 选择，可选 `image_index` 限定图片 |
| `image_and_clone` | 同时指定 `image_index` 和 `clone_id` |

独立补偿任务需要提供已有检测结果：

```json
"compensate": {
  "input_detect_json": "C:/colony_system/data/http_tests/pipeline_C3_detect_http_001/C3/detect_result.json",
  "selector": {
    "mode": "image_and_clone",
    "image_index": 4,
    "clone_id": "C01"
  }
}
```

### 闭环补偿

闭环补偿会在第一次移动后再次拍图、识别、判断是否还需要继续补偿。开启闭环时必须提供闭环图片保存目录，或者在任务中提供 `capture.save_dir`。

```json
"closed_loop": {
  "enabled": true,
  "save_dir": "C:/colony_system/data/compensate_eval/closed_loop/C3_index04_c01",
  "filename_pattern": "closed_loop_{task_id}_{well}_iter{iteration:02d}.bmp",
  "max_iterations": 2,
  "tolerance_px": 10,
  "detect_entrypoint": "vision.vision.detect_pipeline:process_image",
  "selector": {
    "mode": "nearest_image_center"
  }
}
```

如果只想验证首次补偿方向和距离，可以先关闭闭环：

```json
"closed_loop": {
  "enabled": false
}
```

## 输出结果

采集任务输出 `scan_result.json`，包含：

- 扫描参考点、视野、重叠率、点位数量
- 每个扫描点的目标坐标和实际运动结果
- 相机参数和图片路径
- 自动对焦决策和首次采图前自动对焦结果
- 运动安全检查结果

检测任务输出 `detect_result.json`，包含：

- 每张图片的克隆数量
- 克隆中心、面积、边框、相对图像中心偏移
- `confidence` 和 `is_valid_for_compensation`
- `is_pickable`、`near_well_border`、`distance_to_well_edge_px/mm`
- 原图中心和 `mm_per_pixel` 换算
- `overlay_image_path`

workflow 归一化后的 `detect_result.json` 当前不会透传 `refine_method`、`edge_refine_success`、`edge_refine_reason`。这些轮廓细化诊断字段保存在 vision 输出目录的 `07_result.json` 中；需要排查径向轮廓或 GrabCut 回退原因时应查看该文件。

vision 输出目录中常见文件：

```text
01_gray.bmp
02_coarse_flat.bmp
03_coarse_binary.bmp
04_refine_density.bmp
05_contour_mask.bmp
06_overlay.bmp
07_result.json
```

多孔任务会在基础保存目录下按孔位拆分，例如：

```text
data/some_task/
├─ C3/
│  ├─ images/
│  ├─ detect_overlays/
│  ├─ scan_result.json
│  └─ detect_result.json
├─ C5/
│  ├─ images/
│  ├─ detect_overlays/
│  ├─ scan_result.json
│  └─ detect_result.json
└─ result.json
```

## 安全机制

### 电机与坐标安全

当前运动代码使用绝对脉冲坐标，但仓库没有自动回零/Homing 流程。启动任何正式任务前，必须由驱动器或现场流程保证坐标零点有效，并确认 `data/objective_state.json` 与真实物镜状态一致。更换电机、编码器、电子齿轮或驱动器后，旧的 `pulses_per_mm`、A1 坐标、物镜/焦点位置、handoff 点位和软件限位均不能直接复用。

电机首次联调建议按以下顺序进行：

1. 使用厂家工具验证硬件急停、正负限位、站号和低速点动。
2. 程序只读状态字、模式和当前位置，不发送运动命令。
3. 一次只测试一个轴，执行低速、小距离正反向运动并测量实际距离。
4. 标定 X/Y 方向、脉冲/mm、绝对零点、机械行程和安全边距。
5. 依次测试 XY、物镜、细准焦、单孔采集、补偿、handoff，最后才测试整板和往复运动。

软件停止不能替代安全等级合格的硬件急停。当前底层 `quick_stop()` 实际发送的是 Shutdown 控制字；更换驱动器时必须根据新驱动器手册重新核对控制字、状态字、寄存器映射和停止行为。

`config/plates.yaml` 中的板型配置包含两类安全控制：

- `stage_limits`：扫描前检查所有计划点是否超过 X/Y 轴安全范围
- `runtime_guard`：执行中检测疑似卡死、实际移动过小、到位误差过大等问题

如果 `abort_on_motion_failure` 为 `true`，运行中发现运动异常会中止任务，并把已完成图片数量和错误信息写入失败结果。

handoff 上下料点位包含独立安全控制：

- `settle_s`：X/Y 轴到达点位后额外等待，确保机械振动稳定后再通知机器人
- `arrival_tolerance_pulse`：X/Y 轴移动返回误差超过阈值时立即失败，不继续进入机器人交互状态

相机控制包含以下保护：

- `open()` 中途失败会释放已创建的 MVS 句柄和 SDK 引用
- MVS SDK 初始化/反初始化使用进程级引用计数，避免一个控制器关闭影响另一个控制器
- 公共相机 SDK 入口使用锁串行化，降低并发 API 请求造成句柄竞争的风险
- 后台录像线程未退出时拒绝强行停止 MVS 录像，避免停止录像和写帧并发
- 拍照和录像均校验 Mono8；录像每帧写入前校验 PixelFormat 和帧长度

## 常见问题

### 改了代码后为什么测试结果没变？

如果已经启动了 `uvicorn workflow.api_server:app`，修改 Python 源码后需要重启服务。只改 JSON/YAML 配置或请求体通常不需要重启。

### PowerShell curl JSON 报错

PowerShell 中建议使用 `ConvertTo-Json` 和 `Invoke-RestMethod`，不要手写复杂转义字符串。示例见上面的 HTTP 请求体。

### overlay 还是黄色框和红色中心点？

这通常说明服务还在运行旧代码，或者当前结果文件是旧任务产物。重启 uvicorn 后重新提交任务，并查看 `overlay_image_path` 是否指向 `*_vision/06_overlay.bmp`。

如需强制使用旧 workflow overlay，可配置：

```json
"detect": {
  "overlay_source": "workflow"
}
```

### 自动对焦没有执行

检查 `config/autofocus.yaml`：

- `enabled` 是否为 `true`
- `trigger.after_objective_switch` 是否开启
- 本次任务物镜是否真的发生切换
- 是否设置了 `always_before_capture` 或 `always_before_capture_objectives`
- `trigger.scope` 是否为 `once_per_well` 或 `once_per_task`
- 当前扫描点是否为该孔的第一个扫描点

### 闭环补偿报 save_dir 错误

如果 `compensate.closed_loop.enabled=true`，必须提供：

```json
"compensate": {
  "closed_loop": {
    "save_dir": "C:/colony_system/data/compensate_eval/closed_loop"
  }
}
```

或者在任务中提供 `capture.save_dir`。

### 串口或相机被占用

确认没有其他进程打开同一个 COM 口或 MVS 相机。录像期间 workflow 会复用共享录像相机；非录像场景下 autofocus 和正式采集会避免提前打开相机导致句柄冲突。

如果是相机选择问题，优先检查 `camera.yaml` 中的 `serial_number`、`ip` 和 `device_index`。程序选择相机的优先级为 `serial_number > ip > device_index`。

### 配置校验失败

`workflow.config_validator` 报错会带具体字段路径，例如 `camera.objective_settings.10x` 或 `handoff.points.robot_exchange.arrival_tolerance_pulse`。优先按字段路径修改 YAML，不要删除校验器；校验器的作用是把现场错误提前暴露在任务启动前。

## 维护建议

- 修改 `.py` 后重启 uvicorn，再重新发任务验证。
- 修改 YAML 后先运行 `python -m workflow.config_validator`。
- 修改相机、视觉、handoff 或自动对焦代码后运行 `pytest`。
- 设备联调时优先保留请求 JSON、`scan_result.json`、`detect_result.json`、`result.json`，便于复现。
- 标定文件建议纳入版本管理，但现场私有参数可用单独配置文件覆盖。
- 代码改动和测试运行产物建议分开提交，避免功能 commit 混入大量 `data/` 输出。
