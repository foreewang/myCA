# 采集流程主入口脚本说明

## 1. 这个脚本是什么

这份脚本可以视为当前项目里“采集流程的总入口”。

它本身不直接控制电机，也不直接操作相机 SDK，而是负责把这些模块串起来：

1. 读取任务文件（JSON/YAML）
2. 读取配置文件（camera / objectives / plates）
3. 组装运行时上下文
4. 生成采集参数
5. 根据观察范围分发到单孔、多孔或整板采集流程
6. 输出结果 JSON

简化理解：

- `config_loader` 负责“把配置准备好”
- `scan_planner` 负责“算去哪里拍”
- `scan_executor` 负责“真的移动并拍照”
- **这个脚本负责“把上面这些东西串起来跑”**

---

## 2. 在整个系统里的位置

推荐把它理解为：

调度 / 命令行
→ 本脚本（采集主入口）
→ `workflow.config_loader`
→ `workflow.scan_planner`
→ `workflow.scan_executor`
→ `workflow.stage_executor` + `workflow.camera_executor`
→ 生成图片和扫描结果 JSON

也就是说，这个脚本属于 **workflow 业务入口层**。

---

## 3. 它解决的核心问题

如果没有这个脚本，上层要自己做很多事：

- 自己读 task.json
- 自己读 camera.yaml / objectives.yaml / plates.yaml
- 自己判断是单孔、多孔还是整板
- 自己调用扫描规划
- 自己调用执行模块
- 自己保存结果

而这份脚本把这些重复工作统一封装起来了。

所以它的价值是：

- 给调度系统一个统一入口
- 给命令行测试一个统一入口
- 给后续扩展识别、回传坐标等流程提供稳定入口层

---

## 4. 支持的任务范围

它目前支持三种 `observe_scope`：

### 4.1 `single_well`
扫描单个孔。

需要任务里提供：
- `target.well_name`

### 4.2 `well_list`
扫描多个指定孔。

需要任务里提供：
- `target.well_list`

### 4.3 `full_plate`
扫描整块板。

不需要手写孔列表，程序会根据板型配置自动生成全部孔位。

---

## 5. 任务执行流程

### 第一步：读取任务文件

脚本入口参数为：

```bash
python run_capture_task.py --task task.json
```

支持 JSON 或 YAML 任务文件。

- 如果是 YAML，直接读取
- 如果是 JSON，会先转成临时 YAML，再交给 `load_runtime_context`

这样做的目的是兼容你当前的配置装配流程。

### 第二步：读取配置文件

默认会从项目 `config/` 目录读取：

- `camera.yaml`
- `objectives.yaml`
- `plates.yaml`

如果命令行显式传了别的路径，则优先使用命令行指定路径。

### 第三步：装配运行时上下文

会调用：

- `load_runtime_context(...)`

返回统一的 `ctx`：

- `ctx["task"]`
- `ctx["camera"]`
- `ctx["objective"]`
- `ctx["plate"]`

### 第四步：整理执行参数

会调用：

- `build_capture_params(ctx)`

把 task / camera / objective 中分散的字段整理成执行层更容易使用的一份 `params`。

### 第五步：按范围分发任务

会调用：

- `run_capture_task(ctx, params)`

再根据 `observe_scope` 分发到：

- `run_single_well_scan_capture(...)`
- `run_well_list_scan_capture(...)`
- `run_full_plate_scan_capture(...)`

### 第六步：执行扫描和拍照

单孔流程会继续调用：

- `scan_planner.plan_single_well_scan(...)`
- `scan_executor.execute_scan_capture(...)`

然后由执行层去调用：

- `stage_executor.move_to_absolute(...)`
- `camera_executor.capture_with_opened_camera(...)`

### 第七步：输出结果

结果会：

1. 打印到终端
2. 按需写到 JSON 文件

输出路径优先级：

1. `--dump-json`
2. `task.output.result_json`
3. `task.scan.output_json`

---

## 6. 主要函数说明

### `load_structured_file(path)`
统一读取 JSON / YAML 文件。

### `task_path_for_runtime_context(task_path)`
如果任务文件是 JSON，则转换成临时 YAML，供后续 runtime context 使用。

### `build_capture_params(ctx)`
从上下文中提取真正执行采集需要的参数。

### `run_single_well_scan_capture(ctx, params)`
执行单孔采集：先规划，再执行。

### `run_well_list_scan_capture(ctx, params, well_list)`
把多孔任务拆成多个单孔任务，逐个执行并汇总结果。

### `run_full_plate_scan_capture(ctx, params)`
根据板型自动生成全部孔位，再复用多孔任务逻辑。

### `run_capture_task(ctx, params)`
任务范围分发入口。

### `main()`
命令行主入口。

---

## 7. 输入和输出

### 输入

#### 任务文件
支持 JSON / YAML。

最少需要包含：

```yaml
 task:
   task_id: t001
   task_type: capture
   plate_type: 12-well
   objective: 4x
   observe_scope: single_well
```

以及和范围相关的字段，例如：

- `target.well_name`
- `target.well_list`

#### 配置文件
- `camera.yaml`
- `objectives.yaml`
- `plates.yaml`

### 输出

#### 单孔扫描
通常会输出：
- 图像目录
- 当前孔的 `scan_result.json`

#### 多孔 / 整板扫描
会输出：
- 每个孔各自的 images 目录
- 每个孔各自的 `scan_result.json`
- 任务级汇总结果 JSON

---

## 8. 典型用法

### 8.1 单孔采集

```bash
python run_capture_task.py --task config/task_capture_single_well.json
```

### 8.2 多孔采集

```bash
python run_capture_task.py --task config/task_capture_well_list.json
```

### 8.3 整板采集

```bash
python run_capture_task.py --task config/task_capture_full_plate.json
```

### 8.4 显式指定输出结果路径

```bash
python run_capture_task.py --task config/task_capture_single_well.json --dump-json outputs/result.json
```

---

## 9. 这个脚本不是做什么的

为了避免理解混乱，这里强调一下：

它 **不是**：

- 底层相机驱动
- 底层电机驱动
- 板几何换算模块
- 单孔扫描点位规划模块
- 识别模块

它 **是**：

- 采集流程总入口
- 任务分发器
- 配置装配后的业务调度层

---

## 10. 目前版本的边界

当前这个整理版只保留了 `task_type == capture` 的采集流程。

也就是说，如果后面你还要接：

- `scan_and_detect`
- `detect_only`
- `scan_detect_and_pick`

那么通常建议继续沿用这个入口模式，在 `main()` / `run_xxx_task()` 这一层继续扩展。

---

## 11. 后续建议

后续如果你继续工程化，建议做三件事：

1. **把这个脚本正式命名成主入口名**
   例如：
   - `run_capture_task.py`
   - `main_capture.py`

2. **把 task_type 分发扩展成统一总入口**
   例如：
   - capture
   - scan_and_detect
   - pick

3. **补日志**
   现在结果会打印和写 JSON，但还缺少更清晰的运行日志，后续联调时会更方便。

---

## 12. 一句话总结

这份脚本就是：

**采集任务的主入口 + 配置装配后的业务调度器。**

你可以把它理解成“上层调度命令真正落到扫描和拍照流程里的第一站”。
