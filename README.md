# Colony System

培养板克隆自动化工作流：位移台、海康工业相机、物镜/调焦轴和第三方自动对焦协同，按任务完成扫描、拍照、识别、补偿和对接。

命令行和 FastAPI 共用同一入口：`workflow/run_task.py` 中的 `execute_task_request`。

## 能做什么

| 能力 | 说明 |
| --- | --- |
| 扫描规划 | 按板型、物镜视野和重叠率生成孔内路径，扫描前检查限位 |
| 采集 | XY 绝对运动 + 海康 MVS 拍照；可选自动对焦 |
| 识别 | 默认 4x 模型实例定位；可显式使用旧规则算法 |
| 补偿 | 把选定克隆移到视野中心；可选闭环复检 |
| 对接 | 机械臂上下料点 `load_in` / `unload_out` |
| HTTP | 单工作线程任务队列、进度、协作式取消 |
| 辅助 | 后台录像、固定孔位往复扫描、硬件占用查询 |

任务类型：`capture`、`pipeline`、`compensate`、`handoff`；观察范围：`single_well`、`well_list`、`full_plate`。

## 仓库结构

```text
colony_system/
├─ workflow/                 # 任务编排与执行
├─ devices/                  # 相机与 Modbus 电机
├─ vision/                   # 检测流水线、模型清单、调试入口
├─ config/                   # 相机、物镜、板型、对焦、对接 YAML
├─ data/                     # 任务索引、运行输出、物镜状态
├─ docs/                     # 使用与联调文档（本 README 的专题拆分）
├─ third_party/XWJJJ260511/  # 第三方自动对焦
├─ tools/                    # 标定与精度测试脚本
├─ start_api.bat             # 工控机单实例启动脚本
└─ README.md
```

仓库不附带可直接运行的 `task_*.json`。CLI 需自建任务文件；HTTP 在请求体 `task` 中提交相同结构。

## 快速开始

1. Python 3.10+，安装运行依赖（4x 推理再选 GPU 或 CPU 其中一套，见 [环境与配置](docs/setup.md)）：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

2. 改完 YAML 先校验：

```powershell
python -m workflow.config_validator
```

3. 本地跑采集，或启动 API（必须 `--workers 1`）：

```powershell
python workflow/run_task.py --task data/task_capture_single_well.json
uvicorn workflow.api_server:app --host 0.0.0.0 --port 8000 --workers 1
```

任务 JSON 示例见 [任务与命令行](docs/tasks.md)。首次驱动新电机前先读 [硬件与安全](docs/hardware.md)。

## 文档

| 文档 | 内容 |
| --- | --- |
| [服务端口与启动说明](服务端口与启动说明.md) | `D:\colony_system` 部署、TCP 8000、启动与验收 |
| [环境与配置](docs/setup.md) | Python / 视觉运行时、相机与电机约定、YAML 校验 |
| [任务与命令行](docs/tasks.md) | 任务字段、CLI 示例、补偿、输出目录 |
| [HTTP API](docs/http-api.md) | 启停服务、任务队列、录像、往复扫描、日志 |
| [视觉检测](docs/vision.md) | 4x 模型契约、去重与单图调试 |
| [硬件与安全](docs/hardware.md) | 联调顺序、限位、自动对焦、精度测试 |
| [常见问题](docs/troubleshooting.md) | 故障排查与维护习惯 |
| [XY 标定测试方案](docs/xy_stage_calibration_test_plan.md) | 当前位移台标定基线 |

算法实现细节见 [`vision/vision/README.md`](vision/vision/README.md)，模型打包格式见 [`vision/models/ipsc_4x/README.md`](vision/models/ipsc_4x/README.md)。
