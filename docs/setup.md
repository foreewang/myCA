# 环境与配置

## Python

代码使用 `X | None` 等语法，最低需要 Python 3.10。工控机部署使用 64 位 Python 3.10，并在项目根目录建虚拟环境：

```bash
python3.10 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
# 二选一；生产不得使用开放下限的 requirements.txt 重装
python -m pip install -r requirements-lock-py310-gpu.txt
# python -m pip install -r requirements-lock-py310-cpu.txt
python -m workflow.deployment_preflight
```

生产锁文件包含应用与对应视觉运行时的全部传递版本。`requirements.txt` 仅用于开发维护；发布候选锁仍须在目标工控机完成模型、MVS 和硬件验收后归档。

生成发布候选或在工控机执行软件门禁时，再叠加安装精确锁定的测试环境并运行完整回归：

```bash
python -m pip install -r requirements-test-lock-py310.txt
python tools/release_gate.py
```

门禁要求核心任务/运动/视觉回归文件存在、至少收集 200 项测试且不得有任何 skip。`requirements-test-lock-py310.txt` 不属于 API 运行时依赖；若生产镜像需最小化，可在完成并归档门禁报告后从最终运行镜像移除测试环境。

## 4x 模型运行时

维护依赖时可分别解析下面两套运行时，不要在同一环境同时安装 GPU 和 CPU 的 onnxruntime；生产安装使用上一节的完整锁文件：

```bash
# 本机 RTX / Python 3.10（CUDA 12.1 + cuDNN 9.1，适配现有驱动）
python -m pip install -r requirements-vision-runtime-gpu.txt

# CPU 开发/诊断
python -m pip install -r requirements-vision-runtime-cpu.txt
```

部署支线不包含训练、导出或数据集评估依赖；这些工作应在开发支线和独立环境中完成。

模型目录、入口和失败语义见 [视觉检测](vision.md)。

## 相机与 SDK

- 工控机海康 MVS Python SDK 导入目录为 `/opt/MVS/Samples/64/Python/MvImport`，并同时写入 `config/camera.yaml` 与 `config/autofocus.yaml` 的 `camera.mvs_python_dir`。
- 该目录须由现场安装海康 Linux MVS 后提供，其中必须包含 `MvCameraControl_class.py`、`CameraParams_header.py` 和 `CameraParams_const.py`；原生库位于 `/opt/MVS/lib/64` 或 `/opt/MVS/lib/aarch64`。项目部署包不会自动生成 SDK 文件。
- 旧字段 `mvs_sdk_path` 只是兼容别名。
- 选相机优先级：`serial_number` > `ip` > `device_index`。
- 生产采集要求 `pixel_format: mono8`。
- 4x 模型检测固定 5120×5120，`camera.resolution.allow_downscale` 必须为 `false`。

## 电机与坐标约定

默认 Modbus RTU：`/dev/ttyUSB0`、`115200`、8N1。现场设备名不同时改 YAML，推荐 `/dev/serial/by-id/` 下的稳定名。运行用户需属于 `dialout` 组。

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
| `plates.yaml` | 板型几何、内径、示教点 `well_teach`、示教 `well_step`、`pulses_per_mm`、`stage_limits`、`runtime_guard` |
| `autofocus.yaml` | 触发策略与第三方对焦参数 |
| `handoff.yaml` | 位移台对接点（供自控放板/取板） |

改完 YAML 先跑机器校验：

```bash
python -m workflow.config_validator
```

只校验一部分：

```bash
python -m workflow.config_validator --objectives config/objectives.yaml
python -m workflow.config_validator --camera config/camera.yaml --objectives config/objectives.yaml
python -m workflow.config_validator --plates config/plates.yaml
python -m workflow.config_validator --autofocus config/autofocus.yaml --objectives config/objectives.yaml --camera config/camera.yaml
python -m workflow.config_validator --handoff config/handoff.yaml
```

校验覆盖的主要项：

- `objectives.yaml`：物镜名/倍率、正且有限的 FOV、切换模式、整数脉冲目标、正整数速度参数、状态引用、Modbus/slave 及两层焦点碰撞限位
- `camera.yaml`：SDK 路径、序列号/IP/index、分辨率、曝光、增益、全部物镜的 `objective_settings`、`trigger_mode`、`pixel_format`
- `plates.yaml`：板型几何、内径、示教点、示教换孔步长、孔内扫描方向、限位、运行保护、旧字段和错误缩进
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
| `workflow/handoff_executor.py` | 把位移台开到对接点 |
| `workflow/plate_geometry.py` | 孔位与脉冲/mm |
| `devices/camera_controller.py` | 海康拍照与录像 |
| `devices/motion/` | Modbus 与 `MotorManager` |
| `vision/run_detect.py` | 单图调试 |
