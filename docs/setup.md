# 环境与配置

## Python

代码使用 `X | None` 等语法，最低需要 Python 3.10。工控机部署使用 64 位 Python 3.10，并在项目根目录建虚拟环境：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

`requirements.txt` 包含设备应用运行依赖，主要包括 FastAPI、uvicorn、pydantic、PyYAML、numpy、OpenCV、Pillow、pymodbus 和 pyserial。

## 4x 模型运行时

推理依赖与应用依赖分开。同一环境只装下面其中一套，不要同时装 GPU 和 CPU 的 onnxruntime：

```powershell
# 本机 RTX / Python 3.10（CUDA 12.1 + cuDNN 9.1，适配现有驱动）
python -m pip install -r requirements-vision-runtime-gpu.txt

# CPU 开发/诊断
python -m pip install -r requirements-vision-runtime-cpu.txt
```

部署支线不包含训练、导出或数据集评估依赖；这些工作应在开发支线和独立环境中完成。

模型目录、入口和失败语义见 [视觉检测](vision.md)。

## 相机与 SDK

- 海康 MVS Python SDK 路径写在 `config/camera.yaml` 的 `camera.mvs_python_dir`。
- 旧字段 `mvs_sdk_path` 只是兼容别名。
- 选相机优先级：`serial_number` > `ip` > `device_index`。
- 生产采集要求 `pixel_format: mono8`。
- 4x 模型检测固定 5120×5120，`camera.resolution.allow_downscale` 必须为 `false`。

## 电机与坐标约定

默认 Modbus RTU：`COM3`、`115200`、8N1。

| 电机 | 角色 |
| --- | --- |
| 1 | 软件 X：列方向 `A1 -> A2`，图像左右 |
| 2 | 软件 Y：行方向 `A1 -> B1`，图像上下 |
| 3 | 细准焦 |
| 4 | 物镜切换 |

X/Y 是软件业务坐标，不按丝杆长短自动判断。

`motion.x_slave/y_slave`、`config/handoff.yaml` 和往复接口都可以改从站号。一旦交换 X/Y，从站、A1、限位、handoff 点、扫描方向和补偿方向必须一起重新标定。

驱动层假定项目硬编码的寄存器映射、32 位高字在前、CiA-402 PP 序列。换驱动器先核 `devices/motion/modbus.py`，不能只改 YAML。

联调顺序和限位见 [硬件与安全](hardware.md)。

## 配置文件

全部在 `config/`：

| 文件 | 用途 |
| --- | --- |
| `camera.yaml` | 相机、分辨率、曝光/增益、物镜覆盖 |
| `objectives.yaml` | 视野、切换点、碰撞限位、状态文件 |
| `plates.yaml` | 板型几何、`pulses_per_mm`、`stage_limits`、`runtime_guard` |
| `autofocus.yaml` | 触发策略与第三方对焦参数 |
| `handoff.yaml` | 上下料对接点 |

改完 YAML 先跑机器校验：

```powershell
python -m workflow.config_validator
```

只校验一部分：

```powershell
python -m workflow.config_validator --camera config/camera.yaml --objectives config/objectives.yaml
python -m workflow.config_validator --plates config/plates.yaml
python -m workflow.config_validator --autofocus config/autofocus.yaml --objectives config/objectives.yaml --camera config/camera.yaml
python -m workflow.config_validator --handoff config/handoff.yaml
```

校验覆盖的主要项：

- `camera.yaml`：SDK 路径、序列号/IP/index、分辨率、曝光、增益、全部物镜的 `objective_settings`、`trigger_mode`、`pixel_format`
- `plates.yaml`：板型几何、轴方向、限位、运行保护、旧字段和错误缩进
- `autofocus.yaml`：触发策略、MVS 配置、关闭自动曝光、物镜覆盖、调焦范围、串口与物镜硬件一致
- `handoff.yaml`：从站、点位、动作引用、`settle_s`、`arrival_tolerance_pulse`

部署包不携带旧电脑的 `data/objective_state.json`。首次硬件任务前必须人工确认物镜和碰撞安全位置；首次成功切换后程序会生成状态文件。此后该文件必须始终与真实物镜一致。

## 代码入口

| 路径 | 职责 |
| --- | --- |
| `workflow/run_task.py` | CLI 与 `execute_task_request` |
| `workflow/api_server.py` | FastAPI |
| `workflow/config_validator.py` | YAML 机器校验 |
| `workflow/scan_planner.py` / `scan_executor.py` | 路径规划与采集 |
| `workflow/stage_executor.py` | XY 同步绝对运动 |
| `workflow/detect_api.py` / `detect_executor.py` | 检测调用与 `detect_result` |
| `workflow/compensate_executor.py` | 补偿位移 |
| `workflow/objective_executor.py` | 物镜与调焦切换 |
| `workflow/handoff_executor.py` | 上下料对接 |
| `workflow/plate_geometry.py` | 孔位与脉冲/mm |
| `devices/camera_controller.py` | 海康拍照与录像 |
| `devices/motion/` | Modbus 与 `MotorManager` |
| `vision/run_detect.py` | 单图调试 |
