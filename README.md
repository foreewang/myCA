# Colony System 使用说明

本项目用于**菌落培养板自动扫描、图像采集、识别与补偿定位**，支持：
- 位移台控制（Modbus RTU）
- 海康相机采图
- 单孔/多孔/整板任务调度
- 视觉识别与坐标回传
- HTTP 接口对外提供任务执行与结果查询

适用对象：
- 开发者（维护算法/流程/设备接入）
- 软件调度交互端（MES/LIMS/上位机/网页后台）
- 没有编程基础的现场使用者（通过配置文件+命令执行）

---

## 1. 项目功能总览

### 1.1 核心流程

1. 读取任务配置（`task`）
2. 根据培养板与物镜参数生成扫描路径
3. 控制位移台逐点移动并拍照
4. 对采集图像执行菌落识别
5. 按策略选择目标菌落并进行补偿移动（可选）
6. 结果写入 JSON，并可通过 API 查询

### 1.2 支持的任务类型

- `capture`：只扫描+拍照
- `pipeline`：可组合 `capture`/`detect`/`compensate`
- `compensate`：基于已有 detect 结果执行补偿

### 1.3 支持的观察范围

- `single_well`：单孔
- `well_list`：指定孔列表
- `full_plate`：整板全部孔位

---

## 2. 目录结构（关键部分）

```text
colony_system/
├─ workflow/                  # 任务编排、执行器、API
│  ├─ run_task.py             # 命令行任务入口（推荐）
│  ├─ api_server.py           # FastAPI 服务
│  ├─ scan_planner.py         # 扫描点规划
│  ├─ scan_executor.py        # 移动+拍照执行
│  ├─ detect_executor.py      # 批量识别执行
│  └─ compensate_executor.py  # 补偿坐标计算与执行
├─ devices/
│  ├─ motion/                 # Modbus 电机控制
│  └─ camera_controller.py    # 相机控制
├─ vision/                    # 视觉算法
├─ config/                    # 相机/物镜/板型/任务模板配置
└─ data/                      # 任务输出与历史结果
```

---

## 3. 给开发者

### 3.1 技术栈与依赖

已在代码中明确使用的第三方库：
- `fastapi`
- `pydantic`
- `Pillow`
- `PyYAML`
- `opencv-python`
- `numpy`

运行 API 服务通常还需要：
- `uvicorn`

硬件侧依赖：
- 海康相机 MVS Python SDK（由 `devices/camera_controller.py` 使用）
- 串口与 Modbus 驱动环境（位移台）

> 当前仓库未提供统一 `requirements.txt`，建议先在虚拟环境中按以上库安装，再根据本地设备 SDK 补充。

### 3.2 主要入口

- 命令行任务入口：`workflow/run_task.py`
- API 服务入口：`workflow/api_server.py`
- 视觉 CLI 调试入口：`vision/run_detect.py`

### 3.3 关键设计说明

- **任务驱动**：所有执行行为由 `task` 配置控制，避免硬编码流程。
- **配置分层**：`camera.yaml`、`objectives.yaml`、`plates.yaml` 与任务模板解耦。
- **安全机制**：
  - 扫描前限位预检查（路径越界立即中止）
  - 运动执行守护（疑似卡死/误差过大可中止）
- **结果可追踪**：各阶段输出 JSON，便于回放与排错。

---

## 4. 给软件调度交互端（HTTP 集成）

### 4.1 启动服务

在项目根目录执行：

```bash
uvicorn workflow.api_server:app --host 0.0.0.0 --port 8000
```

### 4.2 接口列表

- `GET /health`
  - 健康检查

- `POST /api/tasks/execute`
  - 提交并执行任务
  - 请求体关键字段：
    - `task`：完整任务对象
    - `camera_path` / `objectives_path` / `plates_path`：可选，覆盖默认配置路径
    - `dump_json`：可选，覆盖任务结果落盘路径
    - `persist_result`：是否写文件

- `GET /api/tasks/{task_id}/status`
  - 查询任务状态摘要

- `GET /api/tasks/{task_id}/result`
  - 查询完整任务结果（优先读取结果文件）

- `GET /api/tasks/{task_id}/wells/{well_name}/images`
  - 查询孔位图像列表与产物路径

- `GET /api/tasks/{task_id}/wells/{well_name}/images/{filename}`
  - 下载指定图像

### 4.3 调度集成建议

- 以 `task_id` 作为业务主键。
- 先调 `execute`，再轮询 `status`，最后拉取 `result`。
- 根据 `result.status` 做重试或人工介入。
- 在你的系统里保存 `result_json_path` 与孔位图像路径，便于追溯。

---

## 5. 给没有编程基础的使用者（命令行方式）

### 5.1 你需要准备的内容

1. 硬件连接正常（相机、位移台、串口）
2. 串口号确认（如 `COM3`）
3. `config` 下基础配置已按设备标定
4. 选好任务模板（如 `config/task_capture_single_well.json`）

### 5.2 最简单执行步骤

1. 打开终端，进入项目目录
2. 执行（示例）：

```bash
python workflow/run_task.py --task config/task_pipeline_well_list_detect.json
```

3. 执行结束后，到任务配置中的 `output.result_json` 路径查看结果
4. 到 `capture.save_dir` 查看图像输出

### 5.3 常见模板说明

- `config/task_capture_single_well.json`：单孔拍照
- `config/task_capture_well_list.json`：多孔拍照
- `config/task_capture_full_plate.json`：整板拍照
- `config/task_pipeline_single_well_detect.json`：单孔拍照+识别
- `config/task_pipeline_well_list_detect.json`：多孔拍照+识别
- `config/task_compensate_single_well.json`：单孔补偿

### 5.4 结果怎么看

任务结果 JSON 常见字段：
- `status`：`success` / `failed`
- `observe_scope`：任务范围
- `capture_result` / `detect_result` / `compensate_result`：各阶段结果
- `wells`：多孔/整板模式下每个孔的子结果

---

## 6. 任务配置速查

一个典型 `task` 里通常包括：

- 基本信息：
  - `task_id`
  - `task_type`
  - `stages`
  - `observe_scope`
- 目标信息：
  - `plate_type`
  - `objective`
  - `target.well_name` 或 `target.well_list`
- 采集与运动：
  - `capture.save_dir`
  - `capture.filename_pattern`
  - `motion.port`
  - `motion.profile_vel / profile_acc / profile_dec`
- 扫描：
  - `scan.overlap`
  - `scan.use_objective_fov`
- 识别与补偿（可选）：
  - `detect.entrypoint`
  - `compensate.selector`
- 输出：
  - `output.result_json`
  - `output.detect_json`
  - `output.compensate_json`

---

## 7. 常见问题（FAQ）

- 为什么任务失败？
  - 先看结果 JSON 的 `error` 字段，再检查串口、相机、路径和配置是否有效。

- 为什么某些板型不能直接用？
  - `plates.yaml` 中若 `a1_start`、孔径、间距为 `null`，说明未标定，需先完成标定。

- 识别入口要怎么改？
  - 在任务配置里设置 `detect.entrypoint`，格式为 `module:function`。

- 多孔任务如何提升效率？
  - 使用 `well_list` 或 `full_plate`，系统会复用相机连接，减少开关设备开销。

---

## 8. 版本与维护建议

- 建议把每次任务模板与结果 JSON 一并归档，便于问题追踪。
- 建议后续补充：
  - `requirements.txt`
  - 启动脚本（Windows `.bat` / Linux `.sh`）
  - 一份“标定流程手册”（板型坐标、物镜焦点、限位参数）

