# Pipeline 主入口使用说明

## 1. 脚本定位

`run_task_pipeline.py` 是新的 pipeline 主入口。

它把任务拆成按阶段执行的流水线：

- `capture`：采集图像
- `detect`：识别克隆，输出中心点坐标
- `compensate`：根据克隆中心点与图像中心点的偏差，控制位移台做 X/Y 补偿

相比旧版只支持 `capture` 的主入口，这版改成了 **stages 驱动**：
同一套主程序可以支持：

- 只采集
- 采集 + 识别
- 采集 + 识别 + 补偿

## 2. 依赖模块

这版 pipeline 复用你已有的模块：

- `workflow.config_loader.load_runtime_context`
- `workflow.scan_planner.plan_single_well_scan`
- `workflow.scan_executor.execute_scan_capture`
- `workflow.detect_executor.execute_detect_on_scan_result`
- `workflow.stage_executor.move_to_absolute`
- `workflow.plate_geometry`

另外新增补偿模块：

- `workflow.compensate_executor.execute_compensate_on_detect_result`

如果你使用我同时给你的 `compensate_executor_pipeline.py`，请把它保存到：

- `workflow/compensate_executor.py`

## 3. 主流程

运行时主链路如下：

`task.json/yaml`
→ `load_runtime_context(...)`
→ `build_pipeline_params(...)`
→ `run_pipeline_task(...)`
→ 根据 `stages` 顺序执行：
- `capture`
- `detect`
- `compensate`

## 4. 命令行用法

```bash
python run_task_pipeline.py --task config/task_capture.json
```

也可以显式指定配置文件：

```bash
python run_task_pipeline.py \
  --task config/task_pipeline.json \
  --camera config/camera.yaml \
  --objectives config/objectives.yaml \
  --plates config/plates.yaml \
  --dump-json outputs/pipeline_result.json
```

## 5. 任务写法示例

### 5.1 只采集

```json
{
  "task": {
    "task_id": "task_capture_a1",
    "task_type": "pipeline",
    "stages": ["capture"],
    "plate_type": "12-well",
    "objective": "4x",
    "observe_scope": "single_well",
    "target": {
      "well_name": "A1"
    },
    "scan": {
      "overlap": 0.1,
      "settle_s": 0.8,
      "use_objective_fov": true,
      "output_json": "outputs/A1/scan_result.json"
    },
    "capture": {
      "save_dir": "outputs/A1/images",
      "filename_pattern": "{task_id}_{well}_{index:02d}.bmp"
    },
    "motion": {
      "port": "COM3",
      "profile_vel": 500000,
      "profile_acc": 100000,
      "profile_dec": 100000,
      "x_slave": 1,
      "y_slave": 2,
      "baudrate": 115200
    },
    "output": {
      "result_json": "outputs/A1/pipeline_result.json"
    }
  }
}
```

### 5.2 采集 + 识别

```json
{
  "task": {
    "task_id": "task_capture_detect_a1",
    "task_type": "pipeline",
    "stages": ["capture", "detect"],
    "plate_type": "12-well",
    "objective": "4x",
    "observe_scope": "single_well",
    "target": {
      "well_name": "A1"
    },
    "scan": {
      "overlap": 0.1,
      "settle_s": 0.8,
      "use_objective_fov": true,
      "output_json": "outputs/A1/scan_result.json"
    },
    "capture": {
      "save_dir": "outputs/A1/images",
      "filename_pattern": "{task_id}_{well}_{index:02d}.bmp"
    },
    "detect": {
      "entrypoint": null,
      "output_json": "outputs/A1/detect_result.json"
    },
    "motion": {
      "port": "COM3",
      "profile_vel": 500000,
      "profile_acc": 100000,
      "profile_dec": 100000,
      "x_slave": 1,
      "y_slave": 2,
      "baudrate": 115200
    },
    "output": {
      "result_json": "outputs/A1/pipeline_result.json"
    }
  }
}
```

### 5.3 采集 + 识别 + 补偿

```json
{
  "task": {
    "task_id": "task_capture_detect_comp_a1",
    "task_type": "pipeline",
    "stages": ["capture", "detect", "compensate"],
    "plate_type": "12-well",
    "objective": "4x",
    "observe_scope": "single_well",
    "target": {
      "well_name": "A1"
    },
    "scan": {
      "overlap": 0.1,
      "settle_s": 0.8,
      "use_objective_fov": true,
      "output_json": "outputs/A1/scan_result.json"
    },
    "capture": {
      "save_dir": "outputs/A1/images",
      "filename_pattern": "{task_id}_{well}_{index:02d}.bmp"
    },
    "detect": {
      "entrypoint": null,
      "output_json": "outputs/A1/detect_result.json"
    },
    "compensate": {
      "output_json": "outputs/A1/compensate_result.json",
      "selector": {
        "mode": "image_and_clone",
        "image_index": 1,
        "clone_id": "C01"
      }
    },
    "motion": {
      "port": "COM3",
      "profile_vel": 500000,
      "profile_acc": 100000,
      "profile_dec": 100000,
      "x_slave": 1,
      "y_slave": 2,
      "baudrate": 115200
    },
    "output": {
      "result_json": "outputs/A1/pipeline_result.json"
    }
  }
}
```

## 6. 补偿目标选择接口

补偿阶段通过 `task.compensate.selector` 指定“补哪一个克隆”。

目前支持：

### 6.1 第一张图第一个克隆

```json
"selector": {
  "mode": "first"
}
```

### 6.2 面积最大克隆

```json
"selector": {
  "mode": "largest_area"
}
```

### 6.3 距离图像中心最近的克隆

```json
"selector": {
  "mode": "nearest_image_center"
}
```

### 6.4 按 clone_id 指定

```json
"selector": {
  "mode": "clone_id",
  "clone_id": "C02"
}
```

也可以配合 `image_index` 一起用：

```json
"selector": {
  "mode": "clone_id",
  "image_index": 3,
  "clone_id": "C02"
}
```

### 6.5 显式指定图像和克隆

```json
"selector": {
  "mode": "image_and_clone",
  "image_index": 3,
  "clone_id": "C01"
}
```

这是最推荐的方式，因为最明确。

## 7. 输出说明

### 7.1 单孔 pipeline 返回

主结果结构中会包含：

- `capture_result`
- `detect_result`
- `compensate_result`

其中未执行的阶段对应字段为 `null`。

### 7.2 多孔 / 整板 pipeline 返回

结果中会有：

- `well_count`
- `wells[]`

每个 `well` 下会记录：

- `capture_result_json`
- `detect_result_json`
- `compensate_result_json`
- `result`

## 8. 当前版本限制

1. `detect` 依赖 `capture`，`compensate` 依赖 `detect`。
   当前版本要求 stages 至少包含 `capture`。

2. 当前补偿只执行一次，不包含“补偿后复拍确认”。
   若要形成更稳的闭环，建议后续加：
   - `verify` 阶段
   - 或 `capture -> detect -> compensate -> capture -> detect` 二次确认

3. 当前 `compensate` 一次只补一个克隆。
   如果后续要批量补多个克隆，建议扩展为：
   - `selector.mode = all`
   - 或支持 `targets: [...]`

## 9. 推荐落地方式

建议你把文件保存为：

- `workflow/compensate_executor.py`
- `workflow/run_task_pipeline.py` 或 `workflow/run_task.py`

如果要替换旧入口，推荐把新脚本命名为：

- `workflow/run_task.py`

这样你原来的调度命令可以逐步切换到新的 pipeline 入口。
