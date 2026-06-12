# Colony System 使用说明

本项目是一套培养板克隆/菌落自动化工作流系统，用于在位移台、海康工业相机、物镜切换轴、调焦轴和第三方自动对焦模块协同下，按任务配置完成：

- 培养孔扫描路径规划
- XY 位移台绝对运动
- 海康 MVS 相机拍照和录像
- 单图/批量克隆识别与轮廓定位
- 可选自动对焦
- 基于识别结果的目标补偿定位
- 机械臂上下料对接位置移动

系统既支持命令行本地执行，也支持 FastAPI HTTP 服务提交任务。核心入口是 `workflow/run_task.py` 中的 `execute_task_request`。

## 项目结构

```text
colony_system/
├─ workflow/                  # 任务编排与执行层
│  ├─ run_task.py              # CLI 入口与 execute_task_request
│  ├─ api_server.py            # FastAPI 服务，任务、结果、图片、录像接口
│  ├─ config_validator.py      # YAML 机器校验，提前拦截配置错误
│  ├─ camera_executor.py       # 相机打开、拍照、共享录像相机管理
│  ├─ autofocus_executor.py    # 第三方自动对焦适配
│  ├─ scan_planner.py          # 孔内扫描点规划与限位预检查
│  ├─ scan_executor.py         # 位移台移动、自动对焦、相机拍照
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
│  ├─ handoff.yaml             # 机械臂上下料对接点
│  └─ task_*.json              # 任务模板
├─ data/                       # 任务索引、运行输出、测试输出
│  └─ objective_state.json     # 当前物镜状态，应与真实硬件状态一致
├─ third_party/XWJJJ260511/    # 第三方自动对焦模块
├─ tools/                      # 测试和辅助脚本
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

### 检测与补偿

当前默认检测入口是：

```text
vision.detect_pipeline:process_image
```

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

补偿阶段会优先使用 `is_pickable=true` 的候选，并跳过 `is_valid_for_compensation=false` 的候选，避免孔边缘、触边、低置信度或异常候选参与补偿。

## 环境准备

建议使用 Python 虚拟环境。当前代码主要依赖：

```text
fastapi
uvicorn
pydantic
pyyaml
numpy
opencv-python
pillow
pymodbus
```

硬件和 SDK 依赖：

- 海康 MVS Python SDK，配置见 `config/camera.yaml`
- `camera.mvs_python_dir` 是 MVS Python SDK 导入目录的标准字段；旧字段 `mvs_sdk_path` 仅作为兼容别名
- 相机选择优先级为 `serial_number > ip > device_index`
- 当前生产采集链路要求 `pixel_format: mono8`
- Modbus RTU 串口设备，默认常见端口为 `COM3`
- XY 位移台从站默认 `x_slave=1`、`y_slave=2`
- 调焦轴默认从站 `3`
- 物镜轴默认从站 `4`

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

在项目根目录执行：

```powershell
cd C:\colony_system
python workflow/run_task.py --task config/task_pipeline_well_list_detect.json
```

可覆盖配置路径：

```powershell
python workflow/run_task.py `
  --task config/task_capture_single_well.json `
  --camera config/camera.yaml `
  --objectives config/objectives.yaml `
  --plates config/plates.yaml `
  --dump-json data/my_result.json
```

handoff 示例：

```powershell
python workflow/run_task.py --task config/task_handoff_load_in.json --handoff config/handoff.yaml
python workflow/run_task.py --task config/task_handoff_unload_out.json --handoff config/handoff.yaml
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

相机拍照和录像测试脚本：

```powershell
python tools/test_camera_photo_video.py `
  --photo-path data/camera_tests/test_capture.bmp `
  --video-path data/camera_tests/test_record.avi `
  --duration-s 5 `
  --fps 10 `
  --bitrate-kbps 1000
```

不加 `--skip-photo` 和 `--skip-video` 时，脚本会测试“后台录像中拍照”。

## HTTP 服务

启动服务：

```powershell
uvicorn workflow.api_server:app --host 0.0.0.0 --port 8000
```

开发时如果只改 Python 代码，可以使用自动重载：

```powershell
uvicorn workflow.api_server:app --host 0.0.0.0 --port 8000 --reload
```

设备联调时不建议使用 `--reload`，因为 reload 会重启进程，可能中断相机、串口、电机任务。

### 任务接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/health` | 健康检查 |
| `POST` | `/api/tasks/execute` | 异步提交任务，返回 accepted |
| `GET` | `/api/tasks/{task_id}/status` | 查询任务状态、进度和当前阶段 |
| `GET` | `/api/tasks/{task_id}/result` | 查询任务结果，运行中时返回进度摘要 |
| `GET` | `/api/tasks/{task_id}/wells/{well_name}/images` | 列出孔位图片和结果文件 |
| `GET` | `/api/tasks/{task_id}/wells/{well_name}/images/{filename}` | 下载孔位图片 |

任务请求体示例：

```json
{
  "task": {
    "task_id": "pipeline_C5_detect_http_001",
    "task_type": "pipeline",
    "stages": ["capture", "detect"],
    "plate_type": "24-well",
    "objective": "4x",
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
      "entrypoint": "vision.detect_pipeline:process_image",
      "output_json": "C:/colony_system/data/http_tests/pipeline_C5_detect_http_001/detect_result.json"
    },
    "output": {
      "result_json": "C:/colony_system/data/http_tests/pipeline_C5_detect_http_001/result.json"
    }
  },
  "persist_result": true
}
```

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

停止录像：

```powershell
Invoke-RestMethod `
  -Uri "http://127.0.0.1:8000/api/camera/record/stop" `
  -Method Post
```

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
| `detect.output_json` | 检测结果 JSON 输出路径 |
| `detect.save_overlay` | 是否保存检测标注图，默认开启 |
| `detect.overlay_source` | `vision` 或 `workflow`，默认 `vision` |
| `detect.detect_well_border` | 是否启用可见孔边界检测，默认开启 |
| `detect.well_border_margin_mm` | 按物理距离判定靠近孔边缘的安全边距 |
| `detect.well_border_margin_px` | 未使用物理边距时的像素安全边距 |
| `compensate.selector` | 补偿目标选择策略 |
| `compensate.scale` | 补偿倍率修正，例如 `{ "x": 0.79, "y": 1.0 }` |
| `compensate.closed_loop` | 闭环补偿配置 |
| `output.result_json` | 总结果 JSON 输出路径 |
| `handoff.action` | `load_in` 或 `unload_out` |

相机参数来自 `camera.yaml`，任务运行时会按当前物镜选择 `camera.objective_settings.<objective>` 中的曝光和增益。底层相机控制器会强制设置并校验 Mono8，录像逐帧也会校验帧格式和长度。

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
  "detect_entrypoint": "vision.detect_pipeline:process_image",
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
- `refine_method`、`edge_refine_success`、`edge_refine_reason`，用于追踪轮廓边缘细化是否成功
- 原图中心和 `mm_per_pixel` 换算
- `overlay_image_path`

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
