# Colony System 使用说明

本项目是一套**菌落培养板自动化工作流**：在 Modbus 位移台与海康工业相机联机的前提下，按任务配置完成**孔内扫描规划 → 逐点运动与采图 → 菌落识别 → 可选补偿定位**；并支持**物镜自动切换**与**机械臂/上下料对接位（handoff）**等扩展动作。

**对外集成方式**

- **HTTP**：`workflow/api_server.py`（FastAPI，同步执行任务并落盘任务索引）
- **命令行**：`workflow/run_task.py`（与 HTTP 共用 `execute_task_request`）
- **生产化 API 草案**：根目录 `openapi_v1.yaml`（OpenAPI 3.0，供调度端导入 Swagger/Postman；与当前 HTTP 路径可并存演进）

**适用读者**

- 开发者（算法、流程、设备、接口）
- 软件调度端（MES/LIMS/上位机）
- 无编程基础使用者（改 JSON 配置 + 一条命令执行）

---

## 1. 功能总览

### 1.1 主流程（`capture` / `pipeline`）

1. 读取任务中的 `task` 与全局配置（相机、物镜、板型）
2. 若任务声明了 `objective`，在执行前通过 `workflow/objective_executor.py` **对齐物镜与调焦轴**（Modbus），并写入 `data/objective_state.json` 等状态
3. `workflow/scan_planner.py` 按孔径、视野、重叠率生成孔内扫描点；可做**限位预检查**
4. `workflow/scan_executor.py`：**位移台移动**（`workflow/stage_executor.py`）→ **相机采图**（`workflow/camera_executor.py` → `devices/camera_controller.py`）
5. 若 `stages` 含 `detect`，`workflow/detect_executor.py` 逐图调用 `workflow/detect_api.py` 所配置的入口（如 `vision.vision.detect_pipeline`）
6. 若含 `compensate`，`workflow/compensate_executor.py` 按策略选克隆并计算补偿位移后再次运动
7. 各阶段 JSON 与总结果落盘；HTTP 侧在 `data/task_index/` 写入任务记录便于查询

### 1.2 独立流程

| `task_type` | 说明 |
|-------------|------|
| `capture` | 仅采集（默认 `stages`: `capture`） |
| `pipeline` | 可配置 `stages`: `capture` / `detect` / `compensate` 组合 |
| `compensate` | 仅补偿（依赖已有 detect 结果 JSON 或内嵌结果） |
| `handoff` | **上下料/对接位**：按 `config/handoff.yaml` 将 XY 台移动到指定动作点位（如 `load_in` / `unload_out`），由 `workflow/handoff_executor.py` 执行 |

### 1.3 观察范围（`observe_scope`，仅扫描类任务）

- `single_well`：单孔
- `well_list`：`target.well_list` 多孔
- `full_plate`：整板所有孔（由 `workflow/plate_geometry.py` 展开）

### 1.4 辅助脚本（非核心服务）

- `vision/run_detect.py`：单图检测调试
- `generate_circle_scan_plan.py`：扫描规划相关工具
- `compare_scan_manifests.py`：扫描清单对比
- `workflow/scan_visualizer.py`：扫描结果可视化

---

## 2. 目录结构（关键部分）

```text
colony_system/
├─ workflow/
│  ├─ run_task.py              # CLI 入口；execute_task_request
│  ├─ api_server.py            # FastAPI：/health、/api/tasks/*
│  ├─ config_loader.py         # 合并 task + camera + objectives + plates → ctx
│  ├─ objective_executor.py    # 物镜/调焦轴 Modbus 切换
│  ├─ handoff_executor.py      # handoff 动作与点位
│  ├─ stage_executor.py        # XY 绝对位移（Modbus + MotorManager）
│  ├─ scan_planner.py          # 单孔扫描路径规划、限位预检
│  ├─ scan_executor.py         # 按规划逐点运动 + 拍照
│  ├─ camera_executor.py       # 海康相机封装（开/关/连续采图）
│  ├─ detect_api.py            # 动态加载 detect.entrypoint
│  ├─ detect_executor.py       # 扫描结果批量检测
│  ├─ compensate_executor.py   # 选点 + 补偿运动
│  ├─ plate_geometry.py        # 孔位几何与板参数
│  └─ scan_visualizer.py
├─ devices/
│  ├─ camera_controller.py     # HikCameraController（MVS SDK）
│  └─ motion/
│     ├─ modbus.py             # Modbus RTU 客户端
│     └─ MotorManager.py       # 单轴 CiA402 风格封装
├─ vision/                     # 菌落检测管线（OpenCV 等）
├─ config/
│  ├─ camera.yaml
│  ├─ objectives.yaml          # 物镜 FOV、硬件切换参数、state 文件路径等
│  ├─ plates.yaml              # 板型、A1 基准、限位、runtime_guard
│  ├─ handoff.yaml             # handoff 点位与 load_in / unload_out 动作
│  └─ task_*.json              # 任务模板示例
├─ data/
│  ├─ task_index/              # API 任务记录（每 task_id 一个 json）
│  └─ ...                      # 运行输出、http_tests 等
├─ openapi_v1.yaml             # 生产化 API v1 OpenAPI 草案
└─ README.md
```

---

## 3. 给开发者

### 3.1 依赖（代码中已使用）

Python 包：`fastapi`、`pydantic`、`Pillow`、`PyYAML`、`opencv-python`、`numpy`；跑 API 常用 `uvicorn`。

硬件/SDK：海康 MVS Python 路径与相机驱动；串口与 Modbus 从站（位移台、物镜/调焦轴等）。

> 仓库未附带统一 `requirements.txt` 时，请按上表在虚拟环境中安装，并补齐厂商 SDK。

### 3.2 入口

| 方式 | 命令 / 模块 |
|------|-------------|
| 本地跑任务 | `python workflow/run_task.py --task <path>` |
| 可选参数 | `--camera`、`--objectives`、`--plates`、`--handoff`、`--dump-json` |
| HTTP 服务 | `uvicorn workflow.api_server:app --host 0.0.0.0 --port 8000` |
| 单图视觉调试 | `python vision/run_detect.py <image.bmp> [--out_dir ...]` |

### 3.3 设计要点

- **任务驱动**：行为由 `task` JSON/YAML 描述，`execute_task_request` 统一调度。
- **物镜**：非 `handoff` 任务在进主流程前会 `ensure_objective_for_task`；结果会 `attach_objective_result` 写回总结果。
- **安全**：板型上可配置 `stage_limits`、`runtime_guard`；扫描前越界点会失败；执行中可检测疑似卡死与到位误差（见 `scan_executor.py`）。
- **检测入口**：`detect.entrypoint` 格式为 `模块路径:函数名`，由 `detect_api` 解析并调用。

### 3.4 与调度端对接

- **当前已实现**：见下文「HTTP 接口」；任务索引目录默认 `data/task_index`，可通过环境变量 `TASK_INDEX_DIR` 覆盖。
- **规划中的契约**：`openapi_v1.yaml`（异步 `jobs`、统一 envelope 等），实现时可与现有 `/api/tasks/*` 并行版本化（如 `/api/v1/...`）。

---

## 4. 给软件调度端（HTTP）

### 4.1 启动

```bash
uvicorn workflow.api_server:app --host 0.0.0.0 --port 8000
```

环境变量（可选）：`TASK_INDEX_DIR`、`CAMERA_CONFIG_PATH`、`OBJECTIVES_CONFIG_PATH`、`PLATES_CONFIG_PATH`（后三项在 `ExecuteTaskRequest` 未传时作为默认配置路径）。

### 4.2 接口一览

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 存活探测 |
| POST | `/api/tasks/execute` | 提交 `task` 并**同步执行**至结束；成功/失败均可能写入 `data/task_index/{task_id}.json` |
| GET | `/api/tasks/{task_id}/status` | 从索引读取状态摘要 |
| GET | `/api/tasks/{task_id}/result` | 优先读 `result_json_path` 文件，否则返回记录内嵌结果 |
| GET | `/api/tasks/{task_id}/wells/{well_name}/images` | 列目录下图片文件名 |
| GET | `/api/tasks/{task_id}/wells/{well_name}/images/{filename}` | 下载单张图 |

请求体（`POST /api/tasks/execute`）字段：`task`（必填）、`camera_path`、`objectives_path`、`plates_path`、`dump_json`、`persist_result`。

**说明**

- `task_type: handoff` 时由 `execute_task_request` 走 `handoff` 分支，**默认**读取 `config/handoff.yaml`；CLI 可通过 `--handoff` 指定其它文件；当前 `api_server` 未暴露 `handoff_path` 字段，若需多环境请在代码中扩展或固定使用默认路径。
- 长耗时任务会长时间占用 HTTP 连接；生产环境建议按 `openapi_v1.yaml` 演进为异步任务 + 轮询。

### 4.3 集成建议

- 用任务里的 `task_id` 与索引文件对应；执行后轮询 `status`，再拉 `result`。
- 在业务系统留存 `result_json_path`、各孔 `capture_result_json` / `detect_result_json` 等路径以便审计。

---

## 5. 给无编程基础使用者（命令行）

### 5.1 准备

1. 相机、位移台、串口连接正常  
2. 确认 `motion.port`（如 `COM3`）与 `config/*.yaml` 已按现场标定  
3. 选好 `config/task_*.json` 模板，按需改路径与孔位列表  

### 5.2 执行示例

```bash
cd C:\colony_system
python workflow/run_task.py --task config/task_pipeline_well_list_detect.json
```

上下料示例：

```bash
python workflow/run_task.py --task config/task_handoff_load_in.json
# 自定义 handoff 配置：
python workflow/run_task.py --task config/task_handoff_unload_out.json --handoff config/handoff.yaml
```

结果：看任务里 `output.result_json`；图片在 `capture.save_dir` 下按 `filename_pattern` 生成。

### 5.3 常用任务模板（`config/`）

| 文件 | 用途 |
|------|------|
| `task_capture_single_well.json` | 单孔拍照 |
| `task_capture_well_list.json` | 多孔拍照 |
| `task_capture_full_plate.json` | 整板拍照 |
| `task_pipeline_single_well_detect.json` | 单孔拍照 + 识别 |
| `task_pipeline_well_list_detect.json` | 多孔拍照 + 识别 |
| `task_compensate_single_well.json` | 单孔补偿 |
| `task_handoff_load_in.json` / `task_handoff_unload_out.json` | 对接位 handoff |

### 5.4 结果 JSON 里常看的字段

- `status`：`success` / `failed`（扫描失败时 `scan_result` 可能带 `error`）
- `observe_scope`、`wells`（多孔/整板）
- `capture_result` / `detect_result` / `compensate_result`
- 物镜切换摘要：结果中由 `objective_executor` 附加的字段（具体键名以运行输出为准）

---

## 6. 任务配置速查

顶层可为 `{"task": {...}}` 或直接兼容 `run_task` 读入的结构；`execute_task_request` 要求存在顶层键 **`task`**。

常用 `task` 字段：

- **通用**：`task_id`、`task_type`、`plate_type`、`objective`（扫描类会触发物镜对齐）
- **扫描类**：`observe_scope`、`target.well_name` / `target.well_list`、`stages`
- **采集**：`capture.save_dir`、`capture.filename_pattern`
- **运动**：`motion.port`、`baudrate`、`x_slave`、`y_slave`、`profile_vel` / `profile_acc` / `profile_dec`
- **扫描**：`scan.overlap`、`scan.use_objective_fov`、`scan.settle_s`
- **识别**：`detect.entrypoint`、`detect.output_json` 等
- **补偿**：`compensate.selector`、`compensate.input_detect_json` 等
- **输出**：`output.result_json` 等
- **handoff**：`handoff.action`（如 `load_in`，与 `config/handoff.yaml` 中 `actions` 一致）

---

## 7. 常见问题（FAQ）

- **任务失败怎么办？**  
  查看返回或落盘 JSON 中的 `error` / `status: failed` 说明；检查串口占用、相机是否被其它进程打开、`plates.yaml` 限位与 A1 坐标是否已标定。

- **某板型不能扫？**  
  `plates.yaml` 里对应板型的 `a1_start`、孔径、间距等若为 `null`，需先标定再跑。

- **如何换检测算法？**  
  设置 `detect.entrypoint` 为 `模块:函数`，函数签名需与 `detect_api` 的调用约定一致（单图路径入参等）。

- **调度端如何对齐 OpenAPI 草案？**  
  导入 `openapi_v1.yaml` 生成客户端或 Mock；实现服务端时再映射到 `execute_task_request` 与队列。

---

## 8. 维护建议

- 版本化保存任务 JSON 与每次 `result.json`，便于复现。  
- 补充 `requirements.txt`、一键启动脚本、板型/物镜/限位**标定操作文档**。  
- 生产环境：异步任务、鉴权、设备互斥锁、结构化日志与指标。
