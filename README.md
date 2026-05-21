# Colony System 使用说明

本项目是一套培养板菌落自动化工作流系统，用于在位移台、海康工业相机、物镜切换轴、调焦轴等硬件协同下，按任务配置完成：

- 培养孔扫描路径规划
- XY 位移台绝对运动
- 相机采图与结果落盘
- 菌落/克隆识别
- 可选自动对焦
- 可选目标补偿定位
- 机械臂上下料对接位移动

系统既支持命令行本地执行，也支持 FastAPI HTTP 服务提交任务。核心入口是 `workflow/run_task.py` 中的 `execute_task_request`。

## 项目结构

```text
colony_system/
├─ workflow/                  # 任务编排与执行层
│  ├─ run_task.py              # CLI 入口与 execute_task_request
│  ├─ api_server.py            # FastAPI 服务，提交/查询/下载任务结果
│  ├─ config_loader.py         # 加载 task、camera、objectives、plates
│  ├─ objective_executor.py    # 物镜轴与调焦轴切换
│  ├─ autofocus_executor.py    # 调用第三方自动对焦模块
│  ├─ scan_planner.py          # 孔内扫描点规划与限位预检查
│  ├─ scan_executor.py         # 位移台移动、自动对焦、相机拍照
│  ├─ detect_executor.py       # 对采集图像批量检测并生成标注图
│  ├─ compensate_executor.py   # 按检测目标计算补偿位移
│  ├─ handoff_executor.py      # 上下料对接位移动
│  └─ plate_geometry.py        # 板型、孔位、脉冲/mm 等几何计算
├─ devices/
│  ├─ camera_controller.py     # 海康 MVS 相机控制封装
│  └─ motion/                  # Modbus RTU 与 MotorManager
├─ vision/
│  ├─ run_detect.py            # 单图检测调试入口
│  └─ vision/                  # 图像检测流水线
├─ config/
│  ├─ camera.yaml              # 相机配置
│  ├─ objectives.yaml          # 4x/10x 物镜视野、切换点、状态文件
│  ├─ plates.yaml              # 6/12/24/48 孔板几何参数和安全限位
│  ├─ autofocus.yaml           # 自动对焦策略与第三方模块配置
│  ├─ handoff.yaml             # 机械臂上下料对接点
│  └─ task_*.json              # 任务模板
├─ data/                       # 任务索引、运行输出、测试输出
│  ├─ ...
│  └─ objective_state.json     # 当前物镜状态，需跟真实物镜状态匹配，否则影响切镜动作
├─ third_party/XWJJJ260511/    # 自动对焦模块
├─ tools/                      # 辅助工具脚本
└─ README.md
```

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

### 物镜与自动对焦

- 非 `handoff` 任务会根据 `task.objective` 读取 `config/objectives.yaml`，必要时通过 Modbus 控制物镜轴和调焦轴。
- 物镜状态默认写入 `data/objective_state.json`，用于判断是否需要切换。
- `workflow/run_task.py` 会读取 `config/autofocus.yaml` 生成 `autofocus_decision`。
- 真正的自动对焦发生在 `scan_executor.py`：第一个扫描点完成 XY 移动并稳定后、第一张拍照前执行。
- 默认触发策略是物镜发生切换后自动对焦；也可在 `config/autofocus.yaml` 中设置强制每次采集前对焦或指定物镜对焦。触发时机在移动位移台使培养板到达观察位之后。

## 环境准备

建议使用 Python 虚拟环境。当前仓库没有统一的根目录 `requirements.txt`，代码中用到的主要 Python 包包括：

```text
fastapi
uvicorn
pydantic
pyyaml
numpy
opencv-python
pillow
```

硬件与 SDK 依赖：

- 海康 MVS Python SDK，配置项见 `config/camera.yaml` 和 `config/autofocus.yaml`
- Modbus RTU 串口设备，默认端口常见为 `COM3`
- XY 位移台从站默认 `x_slave=1`、`y_slave=2`
- 调焦轴默认从站 `3`
- 物镜轴默认从站 `4`

## 命令行使用

在项目根目录执行：

```bash
cd C:\colony_system
python workflow/run_task.py --task config/task_pipeline_well_list_detect.json
```

可选参数：

```bash
python workflow/run_task.py ^
  --task config/task_capture_single_well.json ^
  --camera config/camera.yaml ^
  --objectives config/objectives.yaml ^
  --plates config/plates.yaml ^
  --dump-json data/my_result.json
```

handoff 示例：

```bash
python workflow/run_task.py --task config/task_handoff_load_in.json --handoff config/handoff.yaml
python workflow/run_task.py --task config/task_handoff_unload_out.json --handoff config/handoff.yaml
```

单图视觉检测调试：

```bash
python vision/run_detect.py path\to\image.bmp
```

## HTTP 服务

启动服务：

```bash
uvicorn workflow.api_server:app --host 0.0.0.0 --port 8000
```

当前 HTTP 执行接口是异步提交：`POST /api/tasks/execute` 返回 `202 Accepted` 后，后台线程执行任务；调用方通过状态接口轮询。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/health` | 健康检查 |
| `POST` | `/api/tasks/execute` | 提交任务，返回 accepted |
| `GET` | `/api/tasks/{task_id}/status` | 查询任务状态、进度和当前阶段 |
| `GET` | `/api/tasks/{task_id}/result` | 查询任务结果，运行中时返回进度摘要 |
| `GET` | `/api/tasks/{task_id}/wells/{well_name}/images` | 列出某孔位图片与结果文件 |
| `GET` | `/api/tasks/{task_id}/wells/{well_name}/images/{filename}` | 下载某张图片 |

请求体示例：

```json
{
  "task": {
    "task_id": "pipeline_well_list_detect_001",
    "task_type": "pipeline",
    "stages": ["capture", "detect"],
    "plate_type": "12-well",
    "objective": "4x",
    "observe_scope": "well_list",
    "target": {
      "well_list": ["A1", "A2", "B1"]
    },
    "capture": {
      "save_dir": "C:/colony_system/data/pipeline_well_list_detect_001",
      "filename_pattern": "{well}_{index:03d}_row{row:02d}_col{col:02d}.bmp"
    },
    "motion": {
      "port": "COM3",
      "baudrate": 115200,
      "x_slave": 1,
      "y_slave": 2,
      "profile_vel": 200000,
      "profile_acc": 50000,
      "profile_dec": 50000,
      "timeout_s": 120.0
    },
    "scan": {
      "overlap": 0.1,
      "use_objective_fov": true,
      "settle_s": 0.8
    },
    "detect": {
      "entrypoint": "vision.vision.detect_pipeline:process_image"
    },
    "output": {
      "result_json": "C:/colony_system/data/pipeline_well_list_detect_001/result.json"
    }
  },
  "persist_result": true
}
```

服务端会在 `data/task_index/{task_id}.json` 写入任务记录。可通过环境变量覆盖默认配置路径：

```text
TASK_INDEX_DIR
CAMERA_CONFIG_PATH
OBJECTIVES_CONFIG_PATH
PLATES_CONFIG_PATH
```

## 任务配置要点

任务文件顶层必须包含 `task` 字段。常见字段如下：

| 字段 | 说明 |
| --- | --- |
| `task_id` | 任务唯一标识，HTTP 任务索引会使用它作为文件名 |
| `task_type` | `capture`、`pipeline`、`compensate` 或 `handoff` |
| `plate_type` | 板型名称，如 `12-well`、`48-well` |
| `objective` | 物镜名称，如 `4x`、`10x` |
| `observe_scope` | `single_well`、`well_list`、`full_plate` |
| `target.well_name` | 单孔任务目标孔位 |
| `target.well_list` | 多孔任务孔位列表 |
| `stages` | 流水线阶段，如 `["capture", "detect"]` |
| `capture.save_dir` | 图片和单孔结果保存目录 |
| `capture.filename_pattern` | 图片命名模板 |
| `motion` | 串口、从站、速度、加减速等运动参数 |
| `scan.overlap` | 扫描重叠率，要求 `0 <= overlap < 1` |
| `scan.use_objective_fov` | 是否使用当前物镜视野生成扫描步长 |
| `detect.entrypoint` | 检测入口，格式为 `模块路径:函数名` |
| `detect.save_overlay` | 是否保存检测标注图，默认开启 |
| `compensate.selector` | 补偿目标选择策略 |
| `output.result_json` | 总结果 JSON 输出路径 |
| `handoff.action` | `load_in` 或 `unload_out` |

## 输出结果

采集任务会输出扫描结果 JSON，包含：

- 扫描参考点、视野、重叠率、点位数量
- 每个扫描点的目标坐标和实际运动结果
- 相机参数与图片路径
- 自动对焦决策和首次采图前自动对焦结果
- 运动安全检查结果

检测任务会输出：

- 每张图片的克隆数量
- 克隆中心、面积、边框、相对图像中心偏移
- 原图中心和 mm/pixel 换算
- 可选 overlay 标注图路径

多孔任务会在基础保存目录下按孔位拆分，例如：

```text
data/some_task/
├─ A1/
│  ├─ images/
│  ├─ scan_result.json
│  └─ detect_result.json
├─ A2/
│  ├─ images/
│  ├─ scan_result.json
│  └─ detect_result.json
└─ result.json
```

## 安全机制

`config/plates.yaml` 中的板型配置包含两类安全控制：

- `stage_limits`：扫描前检查所有计划点是否越过 X/Y 轴安全范围。
- `runtime_guard`：执行中检测疑似卡死、实际移动过小、到位误差过大等问题。

如果 `abort_on_motion_failure` 为 `true`，运行中发现运动异常会中止任务，并把已完成图片数量和错误信息写入失败结果。

## 常用模板

| 文件 | 用途 |
| --- | --- |
| `config/task_capture_single_well.json` | 单孔采集 |
| `config/task_capture_well_list.json` | 多孔采集 |
| `config/task_capture_full_plate.json` | 整板采集 |
| `config/task_pipeline_single_well_detect.json` | 单孔采集并检测 |
| `config/task_pipeline_well_list_detect.json` | 多孔采集并检测 |
| `config/task_compensate_single_well.json` | 单孔补偿 |
| `config/task_align_from_detect.json` | 基于检测结果对齐 |
| `config/task_handoff_load_in.json` | 移动到上料对接位 |
| `config/task_handoff_unload_out.json` | 移动到下料对接位 |

## 常见问题

### 任务提交后没有立即完成

HTTP 接口是异步执行。`POST /api/tasks/execute` 只表示任务已接收，需要继续访问 `/api/tasks/{task_id}/status` 或 `/api/tasks/{task_id}/result`。

### 扫描前提示越界

检查 `config/plates.yaml` 中对应板型的 `a1_start`、孔径、孔间距、`pulses_per_mm`、方向符号和 `stage_limits`。扫描计划会在移动前一次性检查所有点。

### 自动对焦没有执行

检查 `config/autofocus.yaml`：

- `enabled` 是否为 `true`
- `trigger.after_objective_switch` 是否开启
- 本次任务物镜是否真的发生切换
- 是否设置了 `always_before_capture` 或 `always_before_capture_objectives`

### 检测算法如何替换

修改任务中的 `detect.entrypoint`。入口格式为：

```text
python.module.path:function_name
```

函数应接收图片路径并返回包含克隆信息的结构化结果。当前默认入口是：

```text
vision.vision.detect_pipeline:process_image
```

### 串口或相机被占用

确认没有其他进程打开同一个 COM 口或 MVS 相机。自动对焦会自行打开相机，所以需要自动对焦时，系统会避免提前打开共享采集相机。

## 维护建议

- 每次变更板型、物镜、相机曝光参数后，保留对应任务 JSON 与结果 JSON，方便复现。
- 生产环境建议补充统一 `requirements.txt` 或安装脚本。
- 标定文件建议纳入版本管理，但现场私有参数可用单独配置文件覆盖。
- 长任务建议通过 HTTP 状态轮询接入调度端，不要假设提交接口会同步返回最终结果。
